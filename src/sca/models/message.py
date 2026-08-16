from datetime import datetime

from sqlalchemy import ForeignKey, Integer, LargeBinary, String, UniqueConstraint
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


class SentMessage(Base, TimestampMixin):
    """Every letter this system put in front of a supplier, kept as it went out.

    The replies were already stored verbatim and our own half of the exchange was
    not, which left the record one-sided: a supplier could be answering a price
    nobody here could still produce, because the order had been revised twice
    since and the composer only ever renders the order as it stands now.

    Recomposing an old letter from today's rows would be worse than keeping
    nothing — it would read as evidence while showing figures that were never
    sent. So the text is written down at the moment it leaves.

    A letter that could not be delivered at all leaves no row: the send raises,
    the request rolls back, and the order stays where it was. A stored letter
    beside an order that was never sent would be the same lie pointing the other
    way. `delivered` is false for the case that does happen — composed and shown
    to the buyer with no mail configured, then carried by hand.
    """

    __tablename__ = "sent_messages"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    purchase_order_id: Mapped[str | None] = mapped_column(String(32), index=True)
    supplier_id: Mapped[str | None] = mapped_column(String(32), index=True)
    # Where it actually went, which is not always the address on the supplier: a
    # redirect routes test mail to one mailbox, and a record saying otherwise
    # would send somebody looking through an inbox the letter never reached.
    to_address: Mapped[str | None] = mapped_column(String(320))
    subject: Mapped[str | None] = mapped_column(String(500))
    body: Mapped[str] = mapped_column(String(20000), nullable=False, default="")
    # order, revision, receipt — what this letter was for. The revision it went
    # out under is on the row beside it, because "PO-5012" alone does not say
    # which set of figures the supplier was looking at.
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="order")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    delivered: Mapped[bool] = mapped_column(nullable=False, default=False)
    provider: Mapped[str] = mapped_column(String(16), nullable=False, default="none")
    # Why it did not go, where it did not. Empty on a delivered message.
    failure: Mapped[str | None] = mapped_column(String(300))
    sent_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)


class Attachment(Base, TimestampMixin):
    """A file that crossed between here and a supplier, kept byte for byte.

    Stored before anything reads it, and never rewritten. An invoice is the
    evidence behind a payment and a packing list is the evidence behind a
    receipt, so when extraction improves the answer has to be recomputed from
    the original rather than requested again from the supplier — who by then has
    moved on, and whose copy is no longer obviously the same copy.

    The bytes live in the database rather than on disk because the disk under
    this service is replaced on every deploy, and an artefact that disappears on
    a restart is not an artefact.

    Exactly one of the two message columns is set. The file we sent a supplier is
    the same kind of evidence as the one they sent us — a specification attached
    to an order is what an argument about the wrong goods turns on — and keeping
    ours somewhere else would leave the drawer showing one side's files only.
    """

    __tablename__ = "attachments"
    # The same file arrives twice: a supplier resends, or a reply quotes the
    # thread. One row per distinct file per message.
    __table_args__ = (
        UniqueConstraint("inbound_message_id", "sha256", name="uq_attachments_message_sha"),
        UniqueConstraint("sent_message_id", "sha256", name="uq_attachments_sent_sha"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    inbound_message_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("inbound_messages.id"), index=True
    )
    sent_message_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("sent_messages.id"), index=True
    )
    filename: Mapped[str] = mapped_column(String(300), nullable=False)
    content_type: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Content addressing, so the same invoice arriving down two paths is
    # recognisable as one document rather than two.
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
