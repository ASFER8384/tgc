from sqlalchemy import Boolean, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from sca.models.base import Base, TimestampMixin, new_id

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
