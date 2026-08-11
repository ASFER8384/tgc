from datetime import datetime

from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from cdp.models.base import Base, JSONType, TimestampMixin, UTCDateTime, new_id


class Segment(Base, TimestampMixin):
    """A segment is a stored *definition*, not a saved list of people. It is
    re-evaluated on demand, so it cannot drift out of date, and it is auditable —
    you can read why someone was in it six weeks ago.

    `required_consent` is not advisory. The compiler adds it to the WHERE clause,
    so a non-compliant audience cannot be produced by forgetting a checkbox at
    campaign time."""

    __tablename__ = "segments"
    __table_args__ = (UniqueConstraint("key", name="uq_segments_key"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    definition: Mapped[dict] = mapped_column(JSONType, nullable=False)
    required_consent: Mapped[str | None] = mapped_column(String(32))


class SegmentMember(Base):
    """Materialised result of the last evaluation, kept only so the console can
    show membership without recomputing. The definition remains the truth."""

    __tablename__ = "segment_members"

    segment_id: Mapped[str] = mapped_column(String(32), ForeignKey("segments.id"), primary_key=True)
    person_id: Mapped[str] = mapped_column(String(32), ForeignKey("persons.id"), primary_key=True)
    evaluated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)


class ActivationRun(Base, TimestampMixin):
    """One push of one segment to one destination. `skipped_no_consent` is
    reported rather than hidden: a campaign that silently drops half its audience
    is indistinguishable from a broken pipeline."""

    __tablename__ = "activation_runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    segment_id: Mapped[str] = mapped_column(String(32), ForeignKey("segments.id"), nullable=False)
    destination: Mapped[str] = mapped_column(String(64), nullable=False)
    requested: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    delivered: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped_no_consent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class ActivationDelivery(Base, TimestampMixin):
    """Per-person delivery log: what went where, when, under which consent basis.
    This is the table you hand to a regulator, and the one that answers "why did
    this customer get that message?"."""

    __tablename__ = "activation_deliveries"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("activation_runs.id"), nullable=False
    )
    person_id: Mapped[str] = mapped_column(String(32), ForeignKey("persons.id"), nullable=False)
    destination: Mapped[str] = mapped_column(String(64), nullable=False)
    consent_basis: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)
