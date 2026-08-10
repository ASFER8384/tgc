from datetime import datetime

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from sca.models.base import Base, JSONType, TimestampMixin, UTCDateTime, new_id

# The exceptions worth a human's attention. Each one is something a buyer would
# otherwise find out about late, by reading a mailbox or by a stockout.
ISSUE_KINDS = (
    "no_acknowledgement",
    "eta_slip",
    "quantity_mismatch",
    "price_mismatch",
    "short_shipment",
    "unparsed_message",
    "supplier_delay",
)

SEVERITIES = ("low", "medium", "high")


class Issue(Base, TimestampMixin):
    """An exception with a suggested action attached.

    The suggestion is the point. "Supplier has not acknowledged in 36 hours" is a
    report; "chase them, their working day starts in 40 minutes" is an action, and
    it is the difference between a dashboard nobody opens and a system that saves
    the buyer an early morning.
    """

    __tablename__ = "issues"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    purchase_order_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("purchase_orders.id"), index=True
    )
    supplier_id: Mapped[str | None] = mapped_column(String(32), index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    severity: Mapped[str] = mapped_column(String(8), nullable=False, default="medium")
    detail: Mapped[str] = mapped_column(String(500), nullable=False)
    suggested_action: Mapped[str] = mapped_column(String(500), nullable=False)
    context: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)

    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    resolution: Mapped[str | None] = mapped_column(String(500))


class AuditLog(Base, TimestampMixin):
    """Who did what, including the automation. An agent that acts on your behalf
    is only acceptable if every action it took can be listed afterwards."""

    __tablename__ = "audit_log"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    entity: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(32), nullable=False)
    meta: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
