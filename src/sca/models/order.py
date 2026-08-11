from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from sca.models.base import Base, JSONType, TimestampMixin, UTCDateTime, new_id

# One way through the process. Every transition is checked, because the whole
# value of the system is that a human can trust the status column without opening
# the mailbox to find out what really happened.
STATUSES = (
    "draft",
    "pending_approval",
    "approved",
    "sent",
    "acknowledged",
    "in_transit",
    "received",
    "cancelled",
)

ALLOWED_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "draft": ("pending_approval", "approved", "cancelled"),
    "pending_approval": ("approved", "cancelled"),
    "approved": ("sent", "cancelled"),
    "sent": ("acknowledged", "cancelled"),
    "acknowledged": ("in_transit", "received", "cancelled"),
    "in_transit": ("received", "cancelled"),
    "received": (),
    "cancelled": (),
}


class PurchaseOrder(Base, TimestampMixin):
    __tablename__ = "purchase_orders"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    number: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    supplier_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("suppliers.id"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="draft")
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="SAR")
    total_value: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    origin: Mapped[str] = mapped_column(String(16), nullable=False, default="forecast")

    requires_approval: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    approval_reason: Mapped[str | None] = mapped_column(String(200))
    approved_by: Mapped[str | None] = mapped_column(String(64))
    approved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    # Scheduled rather than immediate: an order approved at 6pm Riyadh is queued
    # for the supplier's next working morning instead of landing overnight.
    scheduled_send_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    acknowledged_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    reminded_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    # What we asked for, and what they came back with. Keeping both is what makes
    # a slipped date visible instead of quietly overwritten.
    expected_delivery_date: Mapped[datetime | None] = mapped_column(UTCDateTime)
    confirmed_delivery_date: Mapped[datetime | None] = mapped_column(UTCDateTime)

    # A revision is the negotiation made visible: the supplier came back with a
    # different price, we countered, and this is which round we are on. Kept on
    # the order rather than derived from the audit log because it is printed on
    # the document the supplier receives.
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    revision_reason: Mapped[str | None] = mapped_column(String(200))

    cancelled_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    cancel_reason: Mapped[str | None] = mapped_column(String(200))

    notes: Mapped[str | None] = mapped_column(String(500))


class PurchaseOrderLine(Base):
    __tablename__ = "purchase_order_lines"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    purchase_order_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("purchase_orders.id"), nullable=False, index=True
    )
    sku: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str] = mapped_column(String(200), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    line_total: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    received_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class Shipment(Base, TimestampMixin):
    __tablename__ = "shipments"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    purchase_order_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("purchase_orders.id"), nullable=False, index=True
    )
    carrier: Mapped[str] = mapped_column(String(32), nullable=False)
    tracking_number: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    eta: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    events: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)


class Document(Base, TimestampMixin):
    """An invoice, packing list or proforma, filed against the order it belongs to.

    Extraction results are stored beside the reference rather than replacing it,
    so a wrong read is corrected by re-extracting, not by re-requesting the file.
    """

    __tablename__ = "documents"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    purchase_order_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("purchase_orders.id"), index=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    filename: Mapped[str] = mapped_column(String(200), nullable=False)
    source_message_id: Mapped[str | None] = mapped_column(String(32))
    extracted: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
