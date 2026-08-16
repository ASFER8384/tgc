"""The supplier facing message for a purchase order.

Composed here rather than in whatever finally sends it, so the wording is the
same down every channel and can be read before anything leaves the building. A
buyer who cannot see the exact text is being asked to trust an automation on
faith, which is how automations get switched off.

Times are written in the supplier's own zone with the zone named. "Please
confirm by Tuesday 09:00" means nothing across six hours of offset, and a
supplier who reads it as their own local time is not being unreasonable.
"""

from datetime import datetime
from decimal import Decimal

from sca.models import PurchaseOrder, PurchaseOrderLine, Supplier
from sca.scheduling.windows import WorkingHours, is_open, local_now, next_open


def compose_receipt_email(
    order: PurchaseOrder,
    supplier: Supplier,
    lines: list[PurchaseOrderLine],
    *,
    now: datetime,
) -> dict:
    """The goods received note, and the shortfall when there is one.

    Sent because a receipt is what tells a supplier they may invoice, and because
    a short delivery has to be on the record before the invoice arrives rather
    than argued about after it. The shortfall is stated per line and in their own
    terms: what was ordered, what turned up, what is outstanding.
    """
    hours = WorkingHours.from_supplier(supplier)
    short = [line for line in lines if line.received_quantity < line.quantity]
    reference = f"{order.number}" + (f" Rev {order.revision}" if order.revision else "")

    body = [f"Dear {supplier.name},", ""]
    if not short:
        body += [
            f"{reference} was received complete on {local_now(hours, now):%d %B %Y}.",
            "",
            "Please invoice against this order.",
        ]
    else:
        body += [
            f"{reference} was received on {local_now(hours, now):%d %B %Y}, "
            "short against the order.",
            "",
            "Ordered against received:",
        ]
        for line in short:
            outstanding = line.quantity - line.received_quantity
            body.append(
                f"  {line.sku:<18} ordered {line.quantity:>8,}  "
                f"received {line.received_quantity:>8,}  outstanding {outstanding:>8,}"
            )
        body += [
            "",
            "Please confirm whether the balance is shipping separately or should be",
            "credited, and invoice only for the quantity delivered.",
        ]

    body += ["", "Kind regards,", "Procurement"]
    return {
        "to": supplier.email,
        "subject": (
            f"{'Short receipt' if short else 'Goods received'} {reference} - {supplier.name}"
        ),
        "body": "\n".join(body),
        "short_lines": len(short),
    }


def compose_order_email(
    order: PurchaseOrder,
    supplier: Supplier,
    lines: list[PurchaseOrderLine],
    *,
    ack_deadline_hours: int,
    now: datetime,
) -> dict:
    """Subject, body and the address it goes to, ready to send or to paste."""
    hours = WorkingHours.from_supplier(supplier)
    revised = order.revision > 0
    reference = f"{order.number}" + (f" Rev {order.revision}" if revised else "")

    subject = (
        f"{'Revised purchase order' if revised else 'Purchase order'} {reference}"
        f" - {supplier.name}"
    )

    # The curve under its line, indented, where a line has one. This is the part
    # a mill actually cuts to — without it somebody reads the split off a screen
    # and retypes it into a reply, which is where a size order goes wrong.
    table = []
    for line in lines:
        table.append(
            f"  {line.sku:<18} {line.description[:28]:<28} "
            f"{line.quantity:>8,} x {Decimal(str(line.unit_price)):>10,.2f} = "
            f"{Decimal(str(line.line_total)):>12,.2f}"
        )
        if line.sizes:
            split = "  ".join(
                f"{size} x {count:,}" for size, count in sorted(line.sizes.items())
            )
            table.append(f"  {'':<18} sizes: {split}")

    # Their working day, not ours, and named so it cannot be misread.
    open_now = is_open(hours, now)
    deadline = local_now(hours, next_open(hours, now))
    delivery = (
        order.confirmed_delivery_date or order.expected_delivery_date
    )

    body = [f"Dear {supplier.name},", ""]
    if revised:
        body += [
            f"This replaces our earlier {order.number}. Reason for the revision:",
            f"  {order.revision_reason}",
            "",
            "Please disregard the previous version and confirm against this one.",
            "",
        ]
    else:
        body += [f"Please accept the following order, {reference}.", ""]

    body += ["Lines:", *table, ""]
    body.append(
        f"Order total: {Decimal(str(order.total_value)):,.2f} {order.currency}"
    )
    if delivery:
        body.append(f"Requested delivery: {delivery.date().isoformat()}")
    body += [
        "",
        f"Please acknowledge within {ack_deadline_hours} working hours, counted in "
        f"your own working time.",
        f"Your local time as this was prepared: {local_now(hours, now):%A %d %B, %H:%M %Z}.",
    ]
    # Only worth saying when it is true. Telling an open supplier when we expect
    # them back reads as a machine talking to itself.
    if not open_now:
        body.append(
            "We know your office is closed. We do not expect a reply before "
            f"{deadline:%A %d %B, %H:%M %Z}."
        )
    body += [
        "",
        "Please reply to this message with your confirmation, the price you are",
        "invoicing, and a firm ship date. A reply that changes any of the three is",
        "read as a revision and will come back to a person here.",
        "",
        "Kind regards,",
        "Procurement",
    ]

    return {
        "to": supplier.email,
        "subject": subject,
        "body": "\n".join(body),
        "reference": reference,
        "supplier_local_time": f"{local_now(hours, now):%H:%M %Z}",
        "supplier_open_now": is_open(hours, now),
        "reply_expected_after": deadline.isoformat(),
    }
