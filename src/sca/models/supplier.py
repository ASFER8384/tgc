from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Index, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from sca.models.base import Base, JSONType, TimestampMixin, UTCDateTime, new_id

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
    # The floor nobody wants to go under, in units, regardless of what the
    # forecast says. Cover in weeks is the better trigger and stays the primary
    # one, but it can only speak where there is a demand figure to divide by —
    # and the items with no history are exactly the new lines and the slow
    # movers a buyer most wants a hard minimum on. Null means "use whatever the
    # global default is", which is not the same as zero: zero is somebody saying
    # this item has no floor.
    min_stock: Mapped[int | None] = mapped_column(Integer)

    # The rest of the buying policy, per item, all null and all falling back to
    # the global setting of the same name.
    #
    # One set of numbers across a catalogue of woven abayas, bolts of silk and
    # printed cartons was always a compromise. Silk is bought deep: the mill
    # quotes long and the price moves. Cartons are bought thin: the warehouse
    # cannot hold them and the supplier is up the road. Saying that needs three
    # thresholds, and the system had one.
    #
    # On the item rather than the category, because the exceptions are what need
    # saying — most lines want the default, and a category rule would still be
    # the wrong number for the one seasonal item inside it.
    reorder_cover_weeks: Mapped[float | None] = mapped_column(Numeric(6, 2))
    target_cover_weeks: Mapped[float | None] = mapped_column(Numeric(6, 2))
    demand_window_weeks: Mapped[float | None] = mapped_column(Numeric(6, 2))

    # The sizes or shades this is sold in, for an item the storefront does not
    # carry. Where Shopify has the product its variants are the authority and
    # this stays empty — two lists of sizes that could disagree is worse than
    # one, and the storefront's is the one a customer actually buys from.
    #
    # It exists because a shop cannot count a rail by size until something names
    # the sizes, and an item created here has nothing to ask. Empty is the
    # normal state and means "counted as one number", not "one size".
    #
    # Plain strings rather than a table of their own: a size is a label on a
    # ticket, it has no price, no stock and no life of its own — everything that
    # varies per size is already keyed by the label where it belongs.
    variants: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)


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


class StockLocation(Base, TimestampMixin):
    """A shelf that can be sold from, and counted separately.

    Stock was one number per item, which was true while everything sold from one
    place. It stopped being true the moment the storefront and the shop were both
    selling: twenty abayas is not a fact about the group, it is ten the website
    can ship, five in one shop and five in another, and only the first of those
    can be promised to somebody online.

    The storefront is a single location because Shopify presents it as one. The
    shops are separate because a customer standing in one of them cannot buy what
    is in the other.

    ``code`` rather than a generated id in the foreign keys, so a stock row says
    "riyadh" and can be read without a join.
    """

    __tablename__ = "stock_locations"

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    # "online" or "retail". The push back to Shopify targets the online shelf
    # alone: telling a storefront it holds stock sitting in a shop it cannot ship
    # from is how a website oversells.
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="retail")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class StockAtLocation(Base, TimestampMixin):
    """What is on one shelf, as against what the group holds.

    The rolled-up total stays on ``StockSnapshot`` and remains what buying reads:
    an order goes to a mill for the group, not for a shelf, and splitting the
    reorder decision per location would order four times over. This table is what
    makes the total truthful and what tells a sale which shelf it came off.
    """

    __tablename__ = "stock_at_location"

    sku: Mapped[str] = mapped_column(String(64), primary_key=True)
    location_code: Mapped[str] = mapped_column(
        String(32), ForeignKey("stock_locations.code"), primary_key=True
    )
    on_hand: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    on_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class ShopifyVariant(Base, TimestampMixin):
    """What the storefront says it holds, mirrored here so the two can be compared.

    The storefront is the record for its own shelf and this table does not argue
    with it. Shopify decrements on checkout, on a refund, on a manual correction
    somebody makes in its admin, and on a fulfilment — none of which this platform
    is told about. Any figure kept here that tried to *lead* Shopify's would be
    wrong within a day and would then oversell the website.

    So the direction is fixed: read from Shopify, write nothing back. The
    storefront's own count becomes the ``online`` shelf, the shops' counts stay
    the platform's, and the group total is what those add up to.

    Variants are kept rather than folded into a per-SKU sum, because a sum cannot
    answer the question a shop actually has — twelve abayas is not twelve abayas,
    it is one Small and eleven Large, and only the first of those is why the size
    somebody wants is missing. The sum is still what stock arithmetic uses; this
    is what a person reads.

    A variant with no SKU is kept too, with ``sku`` null. It is a real and common
    fault — a product added in Shopify's admin without one — and it means those
    units belong to no item here and are in no total. Dropping the row would hide
    it; matching it on the product title would invent an answer.
    """

    __tablename__ = "shopify_variants"
    __table_args__ = (Index("ix_shopify_variants_sku", "sku"),)

    # Shopify's own id, so a re-pull updates rather than duplicates, and so the
    # row can be found again in their admin.
    variant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    product_id: Mapped[str] = mapped_column(String(64), nullable=False)
    sku: Mapped[str | None] = mapped_column(String(64))

    product_title: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    handle: Mapped[str | None] = mapped_column(String(300))
    # What the brand mapping keys off. Kept beside the stock so a line arriving
    # under a vendor nobody has mapped can be seen here rather than only in the
    # events it silently failed to file.
    vendor: Mapped[str | None] = mapped_column(String(120))
    status: Mapped[str | None] = mapped_column(String(16))

    # "Small / Black" as Shopify renders it, and the same thing structured. Both,
    # because the string is what a person recognises and the pairs are what a
    # size curve can be counted from.
    variant_title: Mapped[str | None] = mapped_column(String(300))
    options: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)

    price: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    currency: Mapped[str | None] = mapped_column(String(3))

    on_hand: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Whether Shopify counts this variant at all. An untracked variant reports
    # zero and sells forever, so its zero must not be read as an empty shelf.
    tracked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    synced_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)


class StockAtVariant(Base, TimestampMixin):
    """What one shelf holds of one size, as against what it holds of the item.

    A third level under the same rule as the two above it: the group's total is
    the sum of its shelves, and a shelf's total is the sum of the variants
    counted on it. Nothing here is derived downward — a shelf is never split
    across sizes by guessing, because a guess about the size curve is exactly
    the fact somebody is trying to establish.

    **Empty means not counted, never zero.** No rows for a shelf is a shelf
    nobody has broken down, and its item-level total stands untouched. This is
    the whole reason for a separate table rather than a column on
    ``StockAtLocation``: a column would need a value for every row on the day it
    was added, and there is no honest value to give it.

    The variant is Shopify's own label for the row — "Small", "Rose" — because
    that string is what the storefront reports, what the till will offer and
    what is printed on the ticket. Storing an id would be tidier and would mean
    a shop counting a rail could not name what it was counting.
    """

    __tablename__ = "stock_at_variant"

    sku: Mapped[str] = mapped_column(String(64), primary_key=True)
    location_code: Mapped[str] = mapped_column(
        String(32), ForeignKey("stock_locations.code"), primary_key=True
    )
    variant: Mapped[str] = mapped_column(String(120), primary_key=True)
    on_hand: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


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
    # Who put that number there. The field is written by two very different
    # things — a person who knows about a launch, and the forecast run every
    # morning — and until this existed the desk could not tell them apart, so it
    # reported a model figure as "typed here, so it holds over the forecast".
    # That is not a cosmetic slip: it invited a buyer to go looking for whoever
    # typed a number nobody typed.
    #
    # Null means neither has claimed it: a row written before this was recorded,
    # or one still at zero. Not "manual", which is the answer that was wrong.
    weekly_forecast_source: Mapped[str | None] = mapped_column(String(16), nullable=True)


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
    # Which shelf this reading is about. Null means the group — every row written
    # before locations existed is one of those, and it is what demand is divided
    # by, so the meaning of the existing rows had to survive the change.
    location_code: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("stock_locations.code"), nullable=True
    )
    on_hand: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    on_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    recorded_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
