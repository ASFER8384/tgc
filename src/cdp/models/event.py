from datetime import datetime
from decimal import Decimal

from sqlalchemy import ForeignKey, Index, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from cdp.models.base import Base, JSONType, TimestampMixin, UTCDateTime, new_id


class RawEvent(Base):
    """Every payload lands here verbatim before anything interprets it.

    This is the layer that makes the rest of the system cheap to change: when the
    identity rules or the trait definitions move, history is replayed from
    raw_events instead of re-integrated from six upstream APIs. `dedupe_key` is
    the idempotency guarantee — every one of these platforms delivers webhooks
    at-least-once, and a double-counted order destroys trust in every number the
    CDP reports."""

    __tablename__ = "raw_events"
    __table_args__ = (UniqueConstraint("dedupe_key", name="uq_raw_events_dedupe_key"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    topic: Mapped[str] = mapped_column(String(64), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(255), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONType, nullable=False)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    error: Mapped[str | None] = mapped_column(Text)


class Event(Base, TimestampMixin):
    """The normalised timeline. One row per thing a customer did, attributed to a
    person. `occurred_at` is event time, not receive time, so a backfill running
    beside live webhooks still orders correctly."""

    __tablename__ = "events"
    __table_args__ = (
        Index("ix_events_person_occurred", "person_id", "occurred_at"),
        Index("ix_events_name_occurred", "name", "occurred_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    raw_event_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("raw_events.id"))
    person_id: Mapped[str] = mapped_column(String(32), ForeignKey("persons.id"), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    # Total order value for purchase events. Per-brand split lives in payload
    # ("brands"), because one basket can span Aleena and Rawash.
    value_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    currency: Mapped[str | None] = mapped_column(String(3))
    channel: Mapped[str | None] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)


# The audit log is one table for the whole platform, defined once on the supplier
# side and re-exported here. Two declarations of the same table name against one
# metadata is an error, and two audit logs against one database would be worse:
# "what happened to this record" is a question nobody should have to ask twice.
from sca.models.issue import AuditLog  # noqa: E402,F401  re-exported, see below

__all__ = ["AuditLog", "Event", "RawEvent"]
