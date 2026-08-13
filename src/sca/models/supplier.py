from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Index, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from sca.models.base import Base, TimestampMixin, UTCDateTime, new_id

# Everything a supplier is coordinated through, and nothing about what they sell.
# The category lives on the item so one mill can supply fabric to two brands.
CHANNELS = ("email", "portal", "edi")


class Supplier(Base, TimestampMixin):
    """A counterparty and, more importantly, the working hours it keeps.

    The timezone fields are not decoration. Half the cost of cross border buying
    is messages that land at 3am local and sit unread for a day, so every send
    and every chase in this system is scheduled against these columns.
    """

    __tablename__ = "suppliers"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    country: Mapped[str | None] = mapped_column(String(2))
    email: Mapped[str | None] = mapped_column(String(320))
    channel: Mapped[str] = mapped_column(String(16), nullable=False, default="email")

    # IANA name, for example Asia/Shanghai. Stored as text and resolved with
    # zoneinfo so daylight saving is the library's problem, not ours.
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="Asia/Riyadh")
    # Days the supplier actually works, as ISO weekday numbers, Monday is 1.
    # A Gulf supplier resting Friday and Saturday and a Chinese mill resting
    # Saturday and Sunday cannot share one hardcoded weekend.
    working_days: Mapped[str] = mapped_column(String(20), nullable=False, default="1,2,3,4,5")
    work_start_hour: Mapped[int] = mapped_column(Integer, nullable=False, default=9)
    work_end_hour: Mapped[int] = mapped_column(Integer, nullable=False, default=17)

    lead_time_days: Mapped[int] = mapped_column(Integer, nullable=False, default=21)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="SAR")
    min_order_value: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class Item(Base, TimestampMixin):
    """A buyable thing: fabric, a finished garment, a carton, a beauty component."""

    __tablename__ = "items"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    sku: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False, default="finished_goods")
    brand: Mapped[str | None] = mapped_column(String(32))
    supplier_id: Mapped[str] = mapped_column(String(32), nullable=False)
    unit: Mapped[str] = mapped_column(String(16), nullable=False, default="pcs")
    # Both constraints bite in real buying: a mill will not cut below a minimum,
    # and a carton holds what it holds.
    moq: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    pack_size: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    unit_cost: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False, default=0)


class SupplierItem(Base, TimestampMixin):
    """What one supplier will make, and on what terms.

    Price, minimum order, pack size and lead time were never properties of the
    product — they are properties of buying it from a particular mill. Holding
    them on the item forced one supplier per SKU, which is not how sourcing
    works: nobody makes everything, one mill does the abaya and another the
    packaging, and the same silk can be quoted by two.

    Splitting them here is what makes a comparison possible at all. The item
    keeps what the thing *is*; this keeps what it costs to get it from them.
    """

    __tablename__ = "supplier_items"
    __table_args__ = (
        UniqueConstraint("supplier_id", "sku", name="uq_supplier_items_supplier_sku"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    supplier_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("suppliers.id"), nullable=False, index=True
    )
    sku: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    unit_cost: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    # Their currency, not ours. Two quotes in different currencies cannot be
    # compared without saying which is which, and converting on the way in would
    # bake today's rate into a price that outlives it.
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="SAR")
    moq: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    pack_size: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # Per SKU, because a mill that turns packaging in ten days may take
    # six weeks over a woven abaya. Null falls back to the supplier's own
    # figure rather than inventing one.
    lead_time_days: Mapped[int | None] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class StockSnapshot(Base, TimestampMixin):
    """Current position per item, as told to us by inventory and the forecast.

    This service does not compute demand. It consumes whatever the inventory and
    forecasting module produces, which is why the columns are deliberately dumb:
    what is here, what is already coming, and what a week is expected to consume.
    """

    __tablename__ = "stock_snapshots"

    sku: Mapped[str] = mapped_column(String(64), primary_key=True)
    on_hand: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    on_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    weekly_forecast: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False, default=0)


class StockLevel(Base):
    """Every stock position this service was ever told, kept rather than replaced.

    The snapshot above answers "what is there now", which is all a buying
    decision needs. It cannot answer "was this sellable last Tuesday", and that
    question turns out to decide whether the demand figure is right.

    When an item is out of stock the sales recorded against it are zero, but the
    demand is not: customers wanted it and could not have it. Averaging those
    zeroes in reports the item as slow moving, which suppresses the reorder that
    would have put it back on the shelf. The item that sold out fastest ends up
    looking like the one worth buying least.

    Correcting that needs to know when the shelf was empty, and nothing else in
    the system records it. So each push is appended here instead of overwriting,
    and a level is taken to hold until the next one contradicts it. Unchanged
    pushes are not stored: a repeated figure adds no information and the reading
    holds forward anyway.

    This only knows what it was told after it started listening. Demand measured
    over a period this ledger does not cover is reported as uncorrected rather
    than quietly corrected with a guess.
    """

    __tablename__ = "stock_levels"
    __table_args__ = (Index("ix_stock_levels_sku_recorded", "sku", "recorded_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    sku: Mapped[str] = mapped_column(String(64), nullable=False)
    on_hand: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    on_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    recorded_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
