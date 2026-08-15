"""One row per customer, per item, per week.

This is the table everything else is built from. Summed over customers it is
demand per item per week, which is what gets bought; read one row at a time it is
what a particular customer is likely to buy, which is what the campaign side
wants. Both answers come out of the same rows, so they cannot contradict one
another.

Two things here decide whether anything downstream is worth reading.

The zeros. A week in which somebody bought nothing is a row, not an absence, and
those rows are most of the table. Building it only from weeks that have sales
produces a panel in which everybody always buys, and a model trained on that will
predict that everybody always buys.

The empty shelf. Sales are zero while an item is out of stock, and demand is not.
A week nobody could buy is not evidence that nobody wanted it. The stock ledger
says how much of each week the item was purchasable, and that number travels with
every row so training can refuse to learn from a week whose real demand nobody
can know.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cdp.models.event import Event
from sca.models import StockLevel

WEEK = timedelta(days=7)


def week_of(moment: datetime, zone: ZoneInfo) -> date:
    """The Monday of the week this moment falls in, locally.

    Locally, because a sale at one in the morning Riyadh time on a Monday is a
    Monday sale to the shop that made it; bucketing by UTC files a slice of every
    week into the one before it.
    """
    local = moment.astimezone(zone).date()
    return local - timedelta(days=local.weekday())


@dataclass
class Row:
    """One customer, one item, one week. The unit the model is trained on."""

    person_id: str
    sku: str
    week: date
    units: int = 0
    orders: int = 0


@dataclass
class ItemWeek:
    """The same week summed over everybody — what the buying desk plans against."""

    sku: str
    week: date
    units: int = 0
    orders: int = 0
    buyers: int = 0
    # How much of the week the item could actually be bought, in days. ``None``
    # means the ledger says nothing about that week, which is not the same as
    # seven and must never be rounded into it — assuming availability nobody
    # recorded is exactly what makes a fast seller look like a slow one.
    sellable_days: float | None = None

    @property
    def stocked_out(self) -> bool:
        return self.sellable_days is not None and self.sellable_days <= 0

    @property
    def unknowable(self) -> bool:
        """Sold nothing, and known to have had nothing to sell. Demand in such a
        week is unknown rather than zero, and training on it as a zero teaches the
        model to stop buying the things that sell out."""
        return self.units == 0 and self.stocked_out


@dataclass
class Panel:
    rows: list[Row]
    items: dict[str, list[ItemWeek]]
    people: list[str]
    first_week: date | None
    last_week: date | None
    # What never reached the panel. Kept beside it rather than in a log: a demand
    # figure is only as good as the share of trade that got into it.
    lines_without_sku: int = 0
    anonymous_units: int = 0
    total_units: int = 0

    @property
    def weeks(self) -> int:
        if self.first_week is None or self.last_week is None:
            return 0
        return int((self.last_week - self.first_week).days / 7) + 1

    @property
    def week_list(self) -> list[date]:
        if self.first_week is None or self.last_week is None:
            return []
        out, cursor = [], self.first_week
        while cursor <= self.last_week:
            out.append(cursor)
            cursor += WEEK
        return out

    @property
    def attributed_share(self) -> float | None:
        if not self.total_units:
            return None
        return 1.0 - (self.anonymous_units / self.total_units)


def _lines(payload: dict) -> list[dict]:
    # The same two shapes ``sca.planning.demand`` reads. Identical on purpose:
    # two readers of one payload disagreeing about where the lines live shows up
    # as a quiet difference between two screens.
    for candidate in (payload, payload.get("order") or {}):
        lines = candidate.get("line_items")
        if isinstance(lines, list):
            return lines
    return []


def _sellable_days(readings: list[StockLevel], start: datetime, end: datetime) -> float | None:
    """Days in the period during which the item had stock.

    Each reading holds until the next one contradicts it, because that is what a
    stock level is — nobody sends a message when nothing changes. So the reading
    in force at the start of the week is the last one taken at or before it, not
    the first one inside it. ``None`` when the ledger says nothing at or before
    the start: treating silence as "in stock" is the same mistake as treating it
    as "out", and it would be invisible in everything computed afterwards.
    """
    before = [r for r in readings if r.recorded_at <= start]
    if not before:
        return None
    inside = sorted(
        (r for r in readings if start < r.recorded_at < end), key=lambda r: r.recorded_at
    )
    level = max(before, key=lambda r: r.recorded_at).on_hand
    sellable = 0.0
    cursor = start
    for reading in inside:
        if level > 0:
            sellable += (reading.recorded_at - cursor).total_seconds()
        level = reading.on_hand
        cursor = reading.recorded_at
    if level > 0:
        sellable += (end - cursor).total_seconds()
    return sellable / 86400.0


async def build(session: AsyncSession, *, now: datetime, timezone: str = "Asia/Riyadh") -> Panel:
    """Read every paid order into the panel, with availability hung off each week.

    All of history rather than a trailing window: how far back to look is a
    question the caller asks, not a property of the table.
    """
    zone = ZoneInfo(timezone)
    events = list(
        await session.scalars(
            select(Event).where(Event.name == "order_paid").order_by(Event.occurred_at)
        )
    )

    cells: dict[tuple[str, str, date], Row] = {}
    weekly: dict[str, dict[date, ItemWeek]] = defaultdict(dict)
    buyers: dict[tuple[str, date], set[str]] = defaultdict(set)
    lines_without_sku = 0
    anonymous_units = 0
    total_units = 0

    for event in events:
        occurred = event.occurred_at
        if occurred.tzinfo is None:
            occurred = occurred.replace(tzinfo=now.tzinfo)
        week = week_of(occurred, zone)
        payload = event.payload or {}
        # A walk-in sale sits on the till's standing record, which is not a
        # person. It still counts towards what the shop sold — dropping it would
        # understate demand for exactly the items that sell for cash — but it is
        # not one customer with a remarkable habit.
        anonymous = bool(payload.get("anonymous"))
        counted: set[str] = set()

        for line in _lines(payload):
            sku = (line.get("sku") or "").strip()
            if not sku:
                # Shipping and gift cards arrive this way and are harmless. A real
                # product does not, and the count is what makes the difference
                # visible instead of silently missing.
                lines_without_sku += 1
                continue
            try:
                quantity = max(int(line.get("quantity") or 1), 0)
            except (TypeError, ValueError):
                quantity = 1

            total_units += quantity
            if anonymous:
                anonymous_units += quantity
            elif event.person_id:
                key = (event.person_id, sku, week)
                cell = cells.get(key)
                if cell is None:
                    cell = cells[key] = Row(person_id=event.person_id, sku=sku, week=week)
                cell.units += quantity
                if sku not in counted:
                    cell.orders += 1
                buyers[(sku, week)].add(event.person_id)

            row = weekly[sku].get(week)
            if row is None:
                row = weekly[sku][week] = ItemWeek(sku=sku, week=week)
            row.units += quantity
            if sku not in counted:
                row.orders += 1
            counted.add(sku)

    if not weekly:
        return Panel(rows=[], items={}, people=[], first_week=None, last_week=None,
                     lines_without_sku=lines_without_sku)

    first = min(min(weeks) for weeks in weekly.values())
    last = week_of(now, zone)

    ledger: dict[str, list[StockLevel]] = defaultdict(list)
    for reading in await session.scalars(
        select(StockLevel).where(StockLevel.sku.in_(list(weekly)))
    ):
        recorded = reading.recorded_at
        if recorded.tzinfo is None:
            reading.recorded_at = recorded.replace(tzinfo=now.tzinfo)
        ledger[reading.sku].append(reading)

    # Every week between the first sale and this one, for every item, including
    # the weeks nothing sold. Those zeros are the majority of the table and they
    # are the whole point of building it this way.
    dense: dict[str, list[ItemWeek]] = {}
    for sku, weeks in weekly.items():
        rows: list[ItemWeek] = []
        cursor = first
        while cursor <= last:
            row = weeks.get(cursor) or ItemWeek(sku=sku, week=cursor)
            row.buyers = len(buyers.get((sku, cursor), ()))
            start = datetime.combine(cursor, datetime.min.time(), tzinfo=zone)
            row.sellable_days = _sellable_days(ledger.get(sku, []), start, start + WEEK)
            rows.append(row)
            cursor += WEEK
        dense[sku] = rows

    return Panel(
        rows=list(cells.values()),
        items=dense,
        people=sorted({row.person_id for row in cells.values()}),
        first_week=first,
        last_week=last,
        lines_without_sku=lines_without_sku,
        anonymous_units=anonymous_units,
        total_units=total_units,
    )
