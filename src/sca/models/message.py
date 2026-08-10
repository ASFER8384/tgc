from datetime import datetime

from sqlalchemy import String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from sca.models.base import Base, JSONType, TimestampMixin, UTCDateTime, new_id

# What a supplier reply turned out to be. Unknown is a first class outcome: a
# message the extractor cannot classify must surface for a human, never be
# guessed into a status change.
MESSAGE_KINDS = (
    "acknowledgement",
    "delay",
    "invoice",
    "packing_list",
    "quote",
    "question",
    "unknown",
)


class InboundMessage(Base, TimestampMixin):
    """Every supplier reply, stored verbatim before anything interprets it.

    Same reasoning as an event store: extraction improves, and when it does the
    whole mailbox is replayed rather than the suppliers being asked to resend a
    year of confirmations. `external_id` is the mail server's message id, which
    is what makes reprocessing the same inbox idempotent.
    """

    __tablename__ = "inbound_messages"
    __table_args__ = (UniqueConstraint("external_id", name="uq_inbound_messages_external_id"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    external_id: Mapped[str] = mapped_column(String(200), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="email")
    from_address: Mapped[str | None] = mapped_column(String(320))
    supplier_id: Mapped[str | None] = mapped_column(String(32), index=True)
    purchase_order_id: Mapped[str | None] = mapped_column(String(32), index=True)
    subject: Mapped[str | None] = mapped_column(String(500))
    body: Mapped[str] = mapped_column(String(20000), nullable=False, default="")
    received_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)

    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    extracted: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    # Below this, the message is filed for a human instead of acted on. An
    # automation that acts on a guess is worse than one that asks.
    confidence: Mapped[float] = mapped_column(nullable=False, default=0.0)
    processed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
