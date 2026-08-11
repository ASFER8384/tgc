from datetime import datetime
from decimal import Decimal

from sqlalchemy import ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from cdp.models.base import Base, UTCDateTime


class ProfileTraits(Base):
    """Derived, never authored. Every column here is recomputed from `events`,
    which is what makes an identity merge correct by construction: repoint the
    events, recompute, done — no reconciliation of pre-aggregated totals."""

    __tablename__ = "profile_traits"

    person_id: Mapped[str] = mapped_column(String(32), ForeignKey("persons.id"), primary_key=True)
    order_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ltv: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    aov: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    first_order_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_order_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    recency_days: Mapped[int | None] = mapped_column(Integer)
    rfm: Mapped[str | None] = mapped_column(String(3))
    brands_purchased: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    event_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    computed_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)


class PersonBrandStat(Base):
    """Per-brand spend, one row per person per brand. A table rather than a JSON
    blob so "bought Aleena, never bought Rawash" compiles to an indexed EXISTS
    instead of a scan — that segment is the entire cross-sell thesis for TGC and
    will be evaluated constantly."""

    __tablename__ = "person_brand_stats"

    person_id: Mapped[str] = mapped_column(String(32), ForeignKey("persons.id"), primary_key=True)
    brand: Mapped[str] = mapped_column(String(64), primary_key=True)
    orders: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    spend: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    last_order_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
