"""Purchase order lifecycle, approval gates and timezone aware sending."""

import hashlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sca.config import Settings, get_settings
from sca.mail import MailError, OutboundFile, OutboundMessage, get_mailer, resolve_recipient
from sca.models import (
    ALLOWED_TRANSITIONS,
    Attachment,
    AuditLog,
    InboundMessage,
    Issue,
    PurchaseOrder,
    PurchaseOrderLine,
    SentMessage,
    StockLevel,
    StockSnapshot,
    Supplier,
)
from sca.orders.compose import compose_order_email, compose_receipt_email
from sca.scheduling.windows import WorkingHours, is_open, next_open, working_hours_between
from sca.whatsapp import (
    MAX_MEDIA_BYTES,
    MediaMessage,
    Sender,
    TemplateMessage,
    TextMessage,
    WhatsAppError,
    get_sender,
)
from sca.whatsapp import resolve_recipient as wa_recipient


class OrderError(ValueError):
    """Raised for an illegal transition or a missing precondition."""


class OrderInputError(OrderError):
    """Raised when the figures sent are self-contradictory.

    Separate from its parent because the two want different answers: an illegal
    transition is a conflict with the order's state and nothing the caller can
    retype, while this is the buyer's own typing and comes back as something to
    fix. Callers that do not care may keep catching OrderError.
    """


def _checked_sizes(line: dict, quantity: int) -> dict[str, int] | None:
    """The size split for a line, but only when it agrees with the quantity.

    A mill cannot cut "18 abayas"; it needs the curve. But a split that adds to
    something other than the line puts two different numbers on one order and
    lets the supplier choose between them, so a disagreement is refused here
    rather than sent. Absent stays absent — no split is not an even split.
    """
    raw = line.get("sizes")
    if not raw:
        return None
    sizes = {str(size): int(count) for size, count in raw.items() if int(count) > 0}
    if not sizes:
        return None
    stated = sum(sizes.values())
    if stated != quantity:
        raise OrderInputError(
            f"{line.get('sku', 'line')}: the sizes add to {stated}, "
            f"but the line orders {quantity}"
        )
    return sizes


class OrderService:
    def __init__(
        self, session: AsyncSession, *, actor: str = "system", settings: Settings | None = None
    ):
        self.session = session
        self.actor = actor
        # The approval threshold reaches this service through here, so an order
        # is gated on the policy in force when it was raised rather than on
        # whatever was on the environment when the process started.
        self.settings = settings or get_settings()

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
                    sizes=_checked_sizes(line, quantity),
                )
            )

        order.total_value = total
        requires, reason = await self._approval_policy(supplier, total)
        order.requires_approval = requires
        order.approval_reason = reason
        order.status = "pending_approval" if requires else "approved"

        # On order from the moment the order exists, not from when it ships: the
        # question the planner asks is "is this need already covered", and a draft
        # sitting in approval covers it.
        await self._move_on_order(
            {line["sku"]: int(line["quantity"]) for line in lines}
        )
        self._audit("order.create", order.id, {"total": str(total), "requires_approval": requires})
        await self.session.flush()
        return order

    # --------------------------------------------------------------- revision
    # A revision rewinds the order deliberately, which is why it is not in
    # ALLOWED_TRANSITIONS: that table describes the forward path, and letting it
    # express "acknowledged back to approved" would legitimise every backward
    # move. Revising is its own operation with its own guard.
    REVISABLE = ("draft", "pending_approval", "approved", "sent", "acknowledged")

    async def revise(
        self, order: PurchaseOrder, lines: list[dict], *, reason: str,
        now: datetime | None = None,
    ) -> PurchaseOrder:
        """Counter a supplier's price or quantity and put the order back in play.

        The approval gate runs again on the new total, because a revision is
        exactly when a value crosses a threshold it did not cross before, and the
        person who approved the original never saw this number.
        """
        now = now or datetime.now(UTC)
        if order.status not in self.REVISABLE:
            raise OrderError(f"cannot revise {order.number} while it is {order.status}")
        if not lines:
            raise OrderError("a revision needs at least one line")

        supplier = await self.session.get(Supplier, order.supplier_id)
        previous_total = Decimal(str(order.total_value or 0))
        previous_status = order.status
        # Captured before the old lines go, so on order moves by the difference
        # rather than being counted twice.
        previous_quantities = await self._order_quantities(order)

        await self.session.execute(
            delete(PurchaseOrderLine).where(PurchaseOrderLine.purchase_order_id == order.id)
        )
        total = Decimal("0.00")
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
                    sizes=_checked_sizes(line, quantity),
                )
            )

        order.total_value = total
        order.revision += 1
        order.revision_reason = reason
        requires, approval_reason = await self._approval_policy(supplier, total)
        order.requires_approval = requires
        order.approval_reason = approval_reason
        order.status = "pending_approval" if requires else "approved"
        # The supplier has not seen this version, so everything they told us about
        # the previous one is stale. Clearing it stops a confirmed date from an
        # earlier price being read as agreement to this one.
        order.sent_at = None
        order.scheduled_send_at = None
        order.acknowledged_at = None
        order.confirmed_delivery_date = None
        # A revision is a fresh clock: the chase should count from when they
        # receive this version, not the one we withdrew.
        order.reminded_at = None

        new_quantities = {line["sku"]: int(line["quantity"]) for line in lines}
        await self._move_on_order(
            {
                sku: new_quantities.get(sku, 0) - previous_quantities.get(sku, 0)
                for sku in previous_quantities.keys() | new_quantities.keys()
            }
        )
        resolved = await self._close_issues(
            order, ("price_mismatch", "eta_slip"),
            f"superseded by revision {order.revision}", now,
        )
        self._audit(
            "order.revise", order.id,
            {
                "revision": order.revision,
                "from_total": str(previous_total),
                "to_total": str(total),
                "from_status": previous_status,
                "reason": reason,
                "issues_closed": resolved,
            },
        )
        await self.session.flush()
        return order

    async def cancel(
        self, order: PurchaseOrder, *, reason: str, now: datetime | None = None
    ) -> PurchaseOrder:
        """Walk away. Available from every stage the transition table allows."""
        now = now or datetime.now(UTC)
        # Released before the transition, while the order still counts as one we
        # placed. A cancelled order covers nothing, so the need becomes visible
        # again on the next refresh, which is the point of cancelling it.
        await self._move_on_order(
            {sku: -quantity for sku, quantity in (await self._order_quantities(order)).items()}
        )
        self._transition(order, "cancelled")
        order.cancelled_at = now
        order.cancel_reason = reason
        resolved = await self._close_issues(order, None, f"order cancelled: {reason}", now)
        self._audit("order.cancel", order.id, {"reason": reason, "issues_closed": resolved})
        await self.session.flush()
        return order

    async def _close_issues(
        self, order: PurchaseOrder, kinds: tuple[str, ...] | None, resolution: str,
        now: datetime,
    ) -> int:
        """Close the open issues this action answers.

        Leaving them behind is how an exception list stops being believed: a buyer
        who has already dealt with something should not keep being told about it.
        """
        query = select(Issue).where(
            Issue.purchase_order_id == order.id, Issue.status == "open"
        )
        if kinds is not None:
            query = query.where(Issue.kind.in_(kinds))
        issues = list(await self.session.scalars(query))
        for issue in issues:
            issue.status = "resolved"
            issue.resolution = resolution
            issue.resolved_at = now
        return len(issues)

    # ------------------------------------------------------------------ stock
    async def _move_on_order(self, changes: dict[str, int]) -> None:
        """Keep the on order figure in step with the orders that exist.

        Without this the planner never learns that it already bought something:
        cover is on hand plus on order, so an order that leaves on order at zero
        suggests the same purchase again on the next refresh, and again after
        that. Ordering the same silk three times is a worse failure than not
        suggesting it at all.
        """
        for sku, delta in changes.items():
            if not delta:
                continue
            snapshot = await self.session.scalar(
                select(StockSnapshot).where(StockSnapshot.sku == sku)
            )
            if snapshot is None:
                continue
            # Never negative. The count is a running total that predates this
            # system, so a correction can arrive against an order it never saw.
            snapshot.on_order = max(0, snapshot.on_order + delta)

    @staticmethod
    def _quantities(lines) -> dict[str, int]:
        totals: dict[str, int] = {}
        for line in lines:
            totals[line.sku] = totals.get(line.sku, 0) + int(line.quantity)
        return totals

    async def _order_quantities(self, order: PurchaseOrder) -> dict[str, int]:
        lines = list(
            await self.session.scalars(
                select(PurchaseOrderLine).where(PurchaseOrderLine.purchase_order_id == order.id)
            )
        )
        return self._quantities(lines)

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

    async def send(
        self, order: PurchaseOrder, *, now: datetime | None = None,
        subject: str | None = None, message: str | None = None,
    ) -> dict:
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

        # Deliver before the status moves. An order marked sent that never left is
        # the one failure this system exists to prevent, so a mail failure has to
        # stop the transition rather than be logged beside it.
        delivery = await self._deliver(order, supplier, now, subject=subject, message=message)

        self._transition(order, "sent")
        order.sent_at = now
        order.scheduled_send_at = None
        self._audit(
            "order.send", order.id, {"channel": supplier.channel, "delivery": delivery}
        )
        await self.session.flush()
        return {"sent": True, "scheduled_send_at": None, "reason": None, "delivery": delivery}

    async def _deliver(
        self, order: PurchaseOrder, supplier: Supplier, now: datetime,
        *, subject: str | None = None, message: str | None = None,
    ) -> dict:
        """Mail the order, when mail is configured at all.

        With no provider set this is a no-op and the order still moves: the status
        machine and the approval gates are useful long before a mailbox is wired
        up, and were the whole system until one was.
        """
        mailer = get_mailer(self.settings)
        lines = list(
            await self.session.scalars(
                select(PurchaseOrderLine).where(PurchaseOrderLine.purchase_order_id == order.id)
            )
        )
        composed = compose_order_email(
            order, supplier, lines,
            ack_deadline_hours=self.settings.ack_reminder_hours,
            now=now,
        )
        text = message or composed["body"]
        heading = subject or composed["subject"]
        kind = "revision" if order.revision else "order"

        # The supplier's own channel, not the one this system happens to have
        # wired. A mill that answers WhatsApp within the hour and email the
        # following week should be reached the fast way, and which way that is
        # is a fact about them rather than about us.
        if supplier.channel == "whatsapp":
            return await self._deliver_whatsapp(
                order, supplier, lines, now, kind=kind, letter=text, heading=heading,
            )

        # Composed and filed even with no mail configured. The order still moves
        # to sent, and the way it reaches the supplier then is a buyer copying
        # this text out — so the record of what they were shown is exactly as
        # worth keeping as it is when the wire carries it.
        if mailer.name == "none":
            reason = "mail is not configured"
            self._file_sent(
                order, supplier, now, subject=heading, body=text, kind=kind,
                to_address=supplier.email, delivered=False,
                provider="none", failure=reason,
            )
            return {"provider": "none", "delivered": False, "reason": reason}

        try:
            recipient = resolve_recipient(supplier.email, self.settings)
            result = await mailer.deliver(
                OutboundMessage(
                    to=recipient,
                    to_name=supplier.name,
                    subject=heading,
                    body=text,
                )
            )
        except MailError as exc:
            # No row for this one, on purpose. Raising rolls the request back, so
            # the order does not move to sent either — and a letter in the record
            # beside an order that was never sent would be the same lie in the
            # other direction. A send that fails leaves the order exactly where
            # it was, which is the honest account of what happened.
            raise OrderError(f"{order.number} was not sent: {exc}") from exc

        self._file_sent(
            order, supplier, now, subject=heading, body=text, kind=kind,
            to_address=result.get("recipient") or supplier.email,
            delivered=bool(result.get("delivered")),
            provider=mailer.name, failure=result.get("reason"),
        )
        return result

    # Meta's customer service window. A business may write in its own words for
    # this long after the other side writes, and not a minute past it.
    WA_WINDOW = timedelta(hours=24)

    async def whatsapp_window(
        self, supplier: Supplier, *, now: datetime | None = None
    ) -> tuple[bool, datetime | None]:
        """Whether free text is allowed, and until when.

        Measured from the supplier's last inbound WhatsApp message, because that
        is what opens it. Checked here rather than left to fail at Meta's edge:
        a refusal that arrives as a failed order, hours later, on a message
        nobody was watching, is the worst place to learn this rule.
        """
        now = now or datetime.now(UTC)
        last = await self.session.scalar(
            select(InboundMessage.received_at)
            .where(
                InboundMessage.supplier_id == supplier.id,
                InboundMessage.source == "whatsapp",
            )
            .order_by(InboundMessage.received_at.desc())
            .limit(1)
        )
        if last is None:
            return False, None
        closes = last + self.WA_WINDOW
        return closes > now, closes

    async def _open_window(
        self, order: PurchaseOrder, now: datetime
    ) -> tuple[Supplier, Sender, str]:
        """Everything a free-form send needs, or the reason it cannot happen.

        Shared by text and files because the rule is about the conversation and
        not about what is being put into it: Meta allows either for the same
        twenty-four hours and refuses either outside them.
        """
        supplier = await self.session.get(Supplier, order.supplier_id)
        if supplier is None:
            raise OrderError(f"{order.number} has no supplier on file")

        open_now, closes = await self.whatsapp_window(supplier, now=now)
        if not open_now:
            raise OrderError(
                "WhatsApp only allows free text for 24 hours after the supplier "
                "writes. " + (
                    f"{supplier.name} last wrote on {closes - self.WA_WINDOW:%d %b %H:%M}, "
                    "so that window has closed"
                    if closes else f"{supplier.name} has never written on WhatsApp"
                ) + " — send the order again to reopen it."
            )

        sender = get_sender(self.settings)
        if sender.name == "none":
            raise OrderError("whatsapp is not configured")
        try:
            recipient = wa_recipient(supplier.phone, self.settings)
        except WhatsAppError as exc:
            raise OrderError(f"{order.number} cannot be sent: {exc}") from exc
        return supplier, sender, recipient

    async def send_whatsapp_text(
        self, order: PurchaseOrder, text: str, *, now: datetime | None = None
    ) -> dict:
        """Write to the supplier in our own words, inside the open window."""
        now = now or datetime.now(UTC)
        body = (text or "").strip()
        if not body:
            raise OrderInputError("a message needs some words in it")
        supplier, sender, recipient = await self._open_window(order, now)
        try:
            delivery = await sender.send_text(TextMessage(to=recipient, text=body))
        except WhatsAppError as exc:
            raise OrderError(f"{order.number} was not sent: {exc}") from exc

        self._file_sent(
            order, supplier, now,
            # No subject on WhatsApp. Naming the order is what lets this turn be
            # read a month later without the row above it.
            subject=f"WhatsApp about {order.number}", body=body, kind="message",
            to_address=delivery.get("recipient") or supplier.phone,
            delivered=bool(delivery.get("delivered")),
            provider=f"whatsapp:{sender.name}", failure=delivery.get("reason"),
        )
        self._audit("order.whatsapp_text", order.id, {"delivery": delivery})
        await self.session.flush()
        return delivery

    async def send_whatsapp_file(
        self, order: PurchaseOrder, *, filename: str, content_type: str,
        content: bytes, caption: str = "", now: datetime | None = None,
    ) -> dict:
        """Send a supplier a file, and keep our copy of exactly what they got.

        The copy is the point. A specification or a revised size chart sent on
        WhatsApp is what an argument about the wrong goods turns on months later,
        and "we sent it" without the bytes is not an answer — the supplier's
        phone is not a record this business controls.
        """
        now = now or datetime.now(UTC)
        if not content:
            raise OrderInputError("that file is empty")
        if len(content) > MAX_MEDIA_BYTES:
            raise OrderInputError(
                f"{filename} is {len(content) / 1e6:.1f}MB. WhatsApp sends up to "
                f"{MAX_MEDIA_BYTES / 1e6:.0f}MB here — send a link, or split it"
            )
        supplier, sender, recipient = await self._open_window(order, now)

        note = (caption or "").strip()
        try:
            delivery = await sender.send_media(MediaMessage(
                to=recipient, filename=filename or "attachment",
                content_type=content_type or "application/octet-stream",
                content=content, caption=note,
            ))
        except WhatsAppError as exc:
            # Nothing filed, as with a failed letter. A file in the record beside
            # a supplier who never received it is the same lie as an order marked
            # sent that never left.
            raise OrderError(f"{order.number}: {filename} was not sent: {exc}") from exc

        message = self._file_sent(
            order, supplier, now,
            subject=f"WhatsApp about {order.number}",
            body=note or f"[sent {filename}]", kind="message",
            to_address=delivery.get("recipient") or supplier.phone,
            delivered=bool(delivery.get("delivered")),
            provider=f"whatsapp:{sender.name}", failure=delivery.get("reason"),
        )
        await self.session.flush()
        self.session.add(Attachment(
            sent_message_id=message.id,
            filename=(filename or "attachment")[:300],
            content_type=(content_type or "")[:120],
            byte_size=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            content=content,
        ))
        self._audit("order.whatsapp_file", order.id, {
            "filename": filename, "bytes": len(content), "delivery": delivery,
        })
        await self.session.flush()
        return delivery | {"filename": filename, "bytes": len(content)}

    async def send_email_message(
        self, order: PurchaseOrder, *, text: str, subject: str | None = None,
        files: list[tuple[str, str, bytes]] | None = None,
        now: datetime | None = None,
    ) -> dict:
        """Write to the supplier by email from inside the conversation.

        No window to check — email has no equivalent of Meta's rule, which is the
        one respect in which it is the easier wire. Everything else matches the
        WhatsApp path deliberately: the same record, the same kept copies, the
        same refusal to file anything when the send fails.
        """
        now = now or datetime.now(UTC)
        body = (text or "").strip()
        files = files or []
        if not body and not files:
            raise OrderInputError("a message needs some words in it, or a file")
        for filename, _, content in files:
            if not content:
                raise OrderInputError(f"{filename} is empty")
        total = sum(len(content) for _, _, content in files)
        if total > self.settings.mail_max_attachment_bytes:
            raise OrderInputError(
                f"those files come to {total / 1e6:.1f}MB. This sends up to "
                f"{self.settings.mail_max_attachment_bytes / 1e6:.0f}MB — send a link, "
                "or fewer at a time"
            )

        supplier = await self.session.get(Supplier, order.supplier_id)
        if supplier is None:
            raise OrderError(f"{order.number} has no supplier on file")
        mailer = get_mailer(self.settings)
        if mailer.name == "none":
            raise OrderError("mail is not configured")

        # Kept on the order's subject so it lands in the supplier's existing
        # thread rather than starting a second one they have to connect by hand.
        heading = (subject or "").strip() or f"{order.number} — message from procurement"
        try:
            recipient = resolve_recipient(supplier.email, self.settings)
            delivery = await mailer.deliver(OutboundMessage(
                to=recipient, to_name=supplier.name, subject=heading,
                body=body or f"Please see the attached {files[0][0]}.",
                attachments=tuple(
                    OutboundFile(filename=name, content_type=mime, content=content)
                    for name, mime, content in files
                ),
            ))
        except MailError as exc:
            raise OrderError(f"{order.number} was not sent: {exc}") from exc

        message = self._file_sent(
            order, supplier, now, subject=heading,
            body=body or f"[sent {', '.join(name for name, _, _ in files)}]",
            kind="message",
            to_address=delivery.get("recipient") or supplier.email,
            delivered=bool(delivery.get("delivered")),
            provider=mailer.name, failure=delivery.get("reason"),
        )
        await self.session.flush()
        seen: set[str] = set()
        for name, mime, content in files:
            digest = hashlib.sha256(content).hexdigest()
            # The same file picked twice in one selection is one attachment, and
            # the unique constraint on the table says so too.
            if digest in seen:
                continue
            seen.add(digest)
            self.session.add(Attachment(
                sent_message_id=message.id,
                filename=(name or "attachment")[:300],
                content_type=(mime or "")[:120],
                byte_size=len(content),
                sha256=digest,
                content=content,
            ))
        self._audit("order.email_message", order.id, {
            "files": [name for name, _, _ in files], "delivery": delivery,
        })
        await self.session.flush()
        return delivery | {"files": [name for name, _, _ in files]}

    async def whatsapp_draft(self, order: PurchaseOrder) -> dict:
        """What the WhatsApp message would say, before it is sent.

        A GET's worth of work, kept beside the send so the two cannot describe
        different messages: the variables are built once, here, and the sender
        fills the same tuple into the template.
        """
        supplier = await self.session.get(Supplier, order.supplier_id)
        if supplier is None:
            raise OrderError(f"{order.number} has no supplier on file")
        lines = list(
            await self.session.scalars(
                select(PurchaseOrderLine).where(PurchaseOrderLine.purchase_order_id == order.id)
            )
        )
        return {
            "to": supplier.phone,
            "supplier": supplier.name,
            "template": self.settings.wa_template_order,
            "language": self.settings.wa_template_language,
            "variables": list(self._whatsapp_variables(order, lines)),
            "configured": self.settings.wa_provider != "none",
            # The letter that will be filed, and that goes out in full the moment
            # they reply. Shown so nobody presses send believing the template is
            # everything the supplier will ever get.
            "letter": compose_order_email(
                order, supplier, lines,
                ack_deadline_hours=self.settings.ack_reminder_hours,
                now=datetime.now(UTC),
            )["body"],
        }

    async def send_whatsapp(
        self, order: PurchaseOrder, *, now: datetime | None = None
    ) -> dict:
        """Send this order on WhatsApp, whatever the supplier's default channel.

        An order still sitting in approval moves to sent, because it has now been
        sent — by this route rather than the other one. An order already gone is
        nudged instead: the message goes again and the status stays where it is,
        since "sent" twice is not a different state.
        """
        now = now or datetime.now(UTC)
        supplier = await self.session.get(Supplier, order.supplier_id)
        if supplier is None:
            raise OrderError(f"{order.number} has no supplier on file")
        if order.status not in ("approved", "sent", "acknowledged", "in_transit"):
            raise OrderError(f"cannot message {order.number} while it is {order.status}")

        lines = list(
            await self.session.scalars(
                select(PurchaseOrderLine).where(PurchaseOrderLine.purchase_order_id == order.id)
            )
        )
        composed = compose_order_email(
            order, supplier, lines,
            ack_deadline_hours=self.settings.ack_reminder_hours,
            now=now,
        )
        delivery = await self._deliver_whatsapp(
            order, supplier, lines, now,
            kind="revision" if order.revision else "order",
            letter=composed["body"], heading=composed["subject"],
        )

        if order.status == "approved":
            self._transition(order, "sent")
            order.sent_at = now
            order.scheduled_send_at = None
            self._audit("order.send", order.id, {"channel": "whatsapp", "delivery": delivery})
        else:
            self._audit("order.whatsapp", order.id, {"delivery": delivery})
        await self.session.flush()
        return delivery

    def _whatsapp_variables(
        self, order: PurchaseOrder, lines: list[PurchaseOrderLine]
    ) -> tuple[str, ...]:
        """The five slots the approved template has, in its order.

        Positional because the template is: Meta matches {{1}} to the first
        parameter and nothing else, so this tuple and the template's wording are
        one thing that has to change together.
        """
        wanted = order.confirmed_delivery_date or order.expected_delivery_date
        return (
            f"{order.number}{f' Rev {order.revision}' if order.revision else ''}",
            str(len(lines)),
            f"{Decimal(str(order.total_value or 0)):,.2f}",
            order.currency,
            wanted.strftime("%d %B %Y") if wanted else "not stated",
        )

    async def _deliver_whatsapp(
        self, order: PurchaseOrder, supplier: Supplier,
        lines: list[PurchaseOrderLine], now: datetime,
        *, kind: str, letter: str, heading: str,
    ) -> dict:
        """Open the conversation on WhatsApp, with the approved template.

        The template carries the order's shape — number, line count, value, the
        date asked for — and not the order itself. Meta caps a template body at
        about a thousand characters with fixed slots, so a line table with a size
        curve under each line does not fit, and a template that tried would not
        be approved.

        The letter is filed anyway, and in full. What the supplier was told and
        what the record shows would otherwise part company at exactly the moment
        the record matters — and the full text is what goes out the moment they
        reply and the free-text window opens.
        """
        sender = get_sender(self.settings)
        delivery: dict
        if sender.name == "none":
            delivery = {
                "provider": "none", "delivered": False,
                "reason": "whatsapp is not configured",
            }
        else:
            variables = self._whatsapp_variables(order, lines)
            try:
                recipient = wa_recipient(supplier.phone, self.settings)
                delivery = await sender.send_template(
                    TemplateMessage(
                        to=recipient,
                        template=self.settings.wa_template_order,
                        language=self.settings.wa_template_language,
                        variables=variables,
                    )
                )
            except WhatsAppError as exc:
                # Same contract as mail: the order does not move if the message
                # did not go, so a supplier is never recorded as told when they
                # were not.
                raise OrderError(f"{order.number} was not sent: {exc}") from exc

        self._file_sent(
            order, supplier, now, subject=heading, body=letter, kind=kind,
            to_address=delivery.get("recipient") or supplier.phone,
            delivered=bool(delivery.get("delivered")),
            provider=f"whatsapp:{sender.name}", failure=delivery.get("reason"),
        )
        return delivery

    def _file_sent(
        self, order: PurchaseOrder, supplier: Supplier | None, now: datetime, *,
        subject: str, body: str, kind: str, to_address: str | None,
        delivered: bool, provider: str, failure: str | None = None,
    ) -> SentMessage:
        """Write down what went out, as it went out.

        Not recomposed on demand later: the composer renders an order as it
        stands, and an order that has been revised twice since would produce a
        letter the supplier never saw, sitting in the record looking like the one
        they answered.
        """
        row = SentMessage(
            purchase_order_id=order.id,
            supplier_id=supplier.id if supplier else None,
            to_address=to_address,
            subject=(subject or "")[:500],
            body=(body or "")[:20000],
            kind=kind,
            revision=order.revision or 0,
            delivered=delivered,
            provider=provider,
            failure=(failure or None) and str(failure)[:300],
            sent_at=now,
        )
        self.session.add(row)
        return row

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
        self, order: PurchaseOrder, received: dict[str, int], *,
        notify: bool = False, now: datetime | None = None,
    ) -> list[Issue]:
        """Book in what arrived and raise a short shipment where it does not match.

        Sends the supplier a receipt note only when asked to. Off by default:
        counting goods in is a warehouse act, and it should not put mail in
        somebody else's inbox as a side effect.
        """
        now = now or datetime.now(UTC)
        lines = list(
            await self.session.scalars(
                select(PurchaseOrderLine).where(PurchaseOrderLine.purchase_order_id == order.id)
            )
        )
        issues: list[Issue] = []
        arrived: dict[str, int] = {}
        released: dict[str, int] = {}
        for line in lines:
            got = int(received.get(line.sku, line.quantity))
            line.received_quantity = got
            arrived[line.sku] = arrived.get(line.sku, 0) + got
            # The whole ordered quantity leaves on order, not just what turned up:
            # a short shipment is settled with the supplier, and leaving the
            # shortfall on order would hide the gap from the next planning run.
            released[line.sku] = released.get(line.sku, 0) - int(line.quantity)
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

        await self._move_on_order(released)
        for sku, quantity in arrived.items():
            snapshot = await self.session.scalar(
                select(StockSnapshot).where(StockSnapshot.sku == sku)
            )
            if snapshot is not None:
                snapshot.on_hand += quantity
                # Appended, not only assigned. A snapshot says what is on the
                # shelf now; the ledger says what was on it in a given week, and
                # that is what lets demand be measured over the days an item
                # could actually be sold. A delivery is the largest stock
                # movement there is, and while it moved the snapshot alone every
                # week looked equally sellable — which is how a sold-out fortnight
                # reaches the forecast as a fortnight of no demand.
                self.session.add(
                    StockLevel(
                        sku=sku,
                        on_hand=snapshot.on_hand,
                        on_order=snapshot.on_order,
                        recorded_at=now,
                    )
                )

        if order.status in ("acknowledged", "in_transit"):
            self._transition(order, "received")

        # Recorded here, told separately. A receipt is what lets a supplier
        # invoice, and a shortfall has to reach them before their invoice reaches
        # us — but booking goods onto a shelf and writing to a supplier are two
        # decisions, and this used to make the second one silently on the buyer's
        # behalf. Nothing on the page said mail had gone, so the only way to find
        # out was to read the supplier's inbox.
        #
        # A mail failure still does not undo the receipt: the goods are on the
        # shelf whether or not the note went out, which is the opposite of
        # sending an order.
        receipt = {"provider": "none", "delivered": False, "reason": "not asked for"}
        if notify:
            supplier = await self.session.get(Supplier, order.supplier_id)
            if supplier is not None:
                try:
                    receipt = await self._deliver_receipt(order, supplier, lines, now)
                except MailError as exc:
                    receipt = {"delivered": False, "reason": str(exc)}

        self._audit(
            "order.receive", order.id,
            {"lines": len(lines), "issues": len(issues), "receipt": receipt},
        )
        await self.session.flush()
        return issues

    async def send_receipt(
        self, order: PurchaseOrder, *, now: datetime | None = None
    ) -> dict:
        """Mail the receipt note for an order already booked in.

        The deliberate half of what `receive` used to do on its own. Separate so
        that a buyer who wants the supplier told presses something that says so,
        and one who is only correcting a count does not write to anybody.
        """
        now = now or datetime.now(UTC)
        if order.status != "received":
            raise OrderError(f"{order.number} has not been received yet")
        supplier = await self.session.get(Supplier, order.supplier_id)
        if supplier is None:
            raise OrderError(f"{order.number} has no supplier on file")
        lines = list(
            await self.session.scalars(
                select(PurchaseOrderLine).where(PurchaseOrderLine.purchase_order_id == order.id)
            )
        )
        try:
            receipt = await self._deliver_receipt(order, supplier, lines, now)
        except MailError as exc:
            raise OrderError(str(exc)) from exc
        self._audit("order.receipt_note", order.id, {"receipt": receipt})
        await self.session.flush()
        return receipt

    async def _deliver_receipt(
        self, order: PurchaseOrder, supplier: Supplier,
        lines: list[PurchaseOrderLine], now: datetime,
    ) -> dict:
        mailer = get_mailer(self.settings)
        if mailer.name == "none":
            return {"provider": "none", "delivered": False, "reason": "mail is not configured"}
        composed = compose_receipt_email(order, supplier, lines, now=now)
        recipient = resolve_recipient(supplier.email, self.settings)
        result = await mailer.deliver(
            OutboundMessage(
                to=recipient, to_name=supplier.name,
                subject=composed["subject"], body=composed["body"],
            )
        ) | {"short_lines": composed["short_lines"]}
        self._file_sent(
            order, supplier, now,
            subject=composed["subject"], body=composed["body"], kind="receipt",
            to_address=result.get("recipient") or supplier.email,
            delivered=bool(result.get("delivered")),
            provider=mailer.name, failure=result.get("reason"),
        )
        return result

    # --------------------------------------------------------------- chasing
    async def sweep_unacknowledged(self, *, now: datetime | None = None) -> list[Issue]:
        """Find orders the supplier has sat on, and raise them once each.

        The reminder is timezone aware in the only way that matters: the suggested
        action says when the supplier is next at their desk, so a buyer is never
        told to chase someone at 2am their time.
        """
        now = now or datetime.now(UTC)
        # Working hours can never exceed elapsed hours, so the wall clock cutoff is
        # a cheap prefilter the database can apply. The real test is below.
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
            # The deadline runs on the supplier's clock, not ours. Sending is
            # already scheduled around their working hours; measuring the reply in
            # raw hours would undo that at the other end of the same round trip.
            working = working_hours_between(hours, order.sent_at, now)
            if working < self.settings.ack_reminder_hours:
                continue

            open_now = is_open(hours, now)
            when = "now, they are at their desks" if open_now else (
                f"from {next_open(hours, now).astimezone(hours.zone):%H:%M %Z} their time"
            )
            waited = int((now - order.sent_at).total_seconds() // 3600)
            issue = Issue(
                purchase_order_id=order.id,
                supplier_id=order.supplier_id,
                kind="no_acknowledgement",
                severity=(
                    "high" if working >= 2 * self.settings.ack_reminder_hours else "medium"
                ),
                detail=(
                    f"{order.number} sent {waited} hours ago with no acknowledgement, "
                    f"{int(working)} of them inside {supplier.name}'s working hours"
                ),
                suggested_action=f"Chase {supplier.name} {when}",
                context={
                    "hours_waited": waited,
                    "working_hours_waited": round(working, 1),
                    "supplier_open": open_now,
                },
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
