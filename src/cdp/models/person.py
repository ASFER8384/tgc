from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from cdp.models.base import Base, TimestampMixin, UTCDateTime, new_id

# An identifier is STRONG when it names one human by itself (email, phone, the
# retailer's own customer id) and WEAK when it names a browser or a shared piece
# of hardware. The distinction is the whole safety mechanism in identity
# resolution: two people are never merged on weak evidence alone.
STRONG_KINDS = frozenset({"email", "phone", "shopify_customer_id", "whatsapp_id"})

# How an identifier came to us. This is not provenance for its own sake: some
# capture circumstances make it plausible that the number belongs to someone
# other than the person we attached it to, and that fact has to survive into the
# moment we decide whether to message her.
CAPTURE_CONTEXTS = ("checkout", "messaging", "activation", "gift", "console", "unknown")

# Contexts where a third party may be behind the identifier. A number written on
# a form at a mall stand may be her friend's; a number on a gift order is often
# the recipient's, not the buyer's. Neither is a reason to distrust the data for
# counting purposes — but both are a reason not to send her a message derived
# from someone else's purchases.
THIRD_PARTY_CONTEXTS = frozenset({"activation", "gift"})
# A cart token is weaker still than a device: it names a single checkout and is
# never seen again. It links an abandoned basket to the order that followed, and
# nothing else — it must never be evidence that two records are one person.
WEAK_KINDS = frozenset({"device_id", "cart_token"})


class Person(Base, TimestampMixin):
    """One human being. Never deleted on merge — the loser keeps its row and
    points at the winner, so every merge is reversible and every previously
    issued person id still resolves."""

    __tablename__ = "persons"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    merged_into_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("persons.id"), nullable=True, index=True
    )
    display_name: Mapped[str | None] = mapped_column(String(255))
    preferred_language: Mapped[str | None] = mapped_column(String(8))
    preferred_channel: Mapped[str | None] = mapped_column(String(32))
    # Not a human. A shop counter needs somewhere to put the sales nobody left a
    # phone number for, or the items in them vanish from demand entirely — but a
    # till that has rung up four hundred baskets is not the best customer in the
    # business, and left unmarked it would top every segment, every audience and
    # every list. Marked here so counting can include it and anything addressed
    # to a person can leave it out.
    synthetic: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class Identifier(Base, TimestampMixin):
    __tablename__ = "identifiers"
    __table_args__ = (
        # One identifier belongs to exactly one person. This constraint is what
        # makes resolution converge instead of forking on every event.
        UniqueConstraint("kind", "value", name="uq_identifiers_kind_value"),
        Index("ix_identifiers_person_id", "person_id"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    person_id: Mapped[str] = mapped_column(String(32), ForeignKey("persons.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    # Normalised form (E.164 phone, folded email). The raw form stays on the event.
    value: Mapped[str] = mapped_column(String(320), nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    # Where it was captured. Nullable for rows written before this existed; an
    # unknown context is treated as ordinary rather than risky, because marking
    # the entire existing base as unsafe would make the flag mean nothing.
    capture_context: Mapped[str | None] = mapped_column(String(32))

    @property
    def third_party_plausible(self) -> bool:
        return self.capture_context in THIRD_PARTY_CONTEXTS


class IdentityMerge(Base, TimestampMixin):
    """Append-only record of every merge, with the evidence that caused it.
    Reversible: repointing loser identifiers back is a data operation, not a
    forensic exercise, because the linking identifier is recorded here."""

    __tablename__ = "identity_merges"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    winner_person_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("persons.id"), nullable=False
    )
    loser_person_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("persons.id"), nullable=False
    )
    linked_by_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    linked_by_value: Mapped[str] = mapped_column(String(320), nullable=False)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    reverted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class MergeReview(Base, TimestampMixin):
    """The human queue. A weak identifier shared between two people who each
    already have strong identifiers of their own is the classic family-iPad case:
    plausible, and wrong often enough that guessing would expose one customer's
    history to another. It is asked, not assumed."""

    __tablename__ = "merge_reviews"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    person_a_id: Mapped[str] = mapped_column(String(32), ForeignKey("persons.id"), nullable=False)
    person_b_id: Mapped[str] = mapped_column(String(32), ForeignKey("persons.id"), nullable=False)
    linked_by_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    linked_by_value: Mapped[str] = mapped_column(String(320), nullable=False)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
