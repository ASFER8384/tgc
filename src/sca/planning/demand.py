"""Weekly demand per SKU, measured from what customers actually bought.

`StockSnapshot.weekly_forecast` is an input: someone types it, or an upstream
module pushes it. This derives one instead, from the paid orders the customer
half of the platform already stores — which is the one place where keeping both
halves on a single database buys a buyer something they can see, rather than
just being tidier to operate.

Deliberately a trailing average and nothing cleverer. Seasonality, trend and
promotion lift are all real, and all of them need more history than a first
season of trading has; a mean over recent weeks is the estimator that is hardest
to be badly wrong with, and it is honest about what it is. The number it returns
is labelled at every layer above so nobody mistakes it for a model.

The one correction it does make is for stockouts. Sales are zero while an item
is unavailable, but demand is not, and dividing by the whole window counts that
absence as indifference. Left uncorrected the effect is perverse rather than
merely imprecise: the faster something sells out, the fewer weeks it is on sale,
the lower its measured rate, and the less of it gets reordered. So the divisor
is the time the item was actually sellable, read from the stock ledger.

Where the ledger does not cover the period, no correction is invented. The
figure is returned as it always was and marked uncorrected, because a demand
number that quietly assumes availability nobody recorded is worse than one that
admits the gap.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# The supplier side reading the customer side. This is the merge doing work:
# before it, this import crossed a service boundary and a network.
from cdp.models.event import Event
from sca.config import get_settings
from sca.models import StockLevel

# Below this there is not enough shelf time to divide by. An item on sale for a
# day is not evidence of a weekly rate; scaling it up produces a number that is
# arithmetic rather than measurement, and it would be the largest number on the
# page. The rate is capped here instead and said to be a floor.
MIN_AVAILABLE_WEEKS = 0.5


@dataclass
class Demand:
    """What was sold, and over how long — kept together because the average
    means nothing without the span it was taken over. Two units a week from
    eight weeks of trading is a signal; the same figure from four days is not."""

    sku: str
    units: int
    weeks: float
    orders: int
    # The whole trading period looked at, before stockouts were taken out of it.
    # Kept beside ``weeks`` so the difference between them is visible: that gap
    # is the time customers wanted this and could not have it.
    span_weeks: float = 0.0
    # measured  — the ledger covered the period and empty days were removed
    # brief     — it was sellable for so little of it that the rate is a floor
    # uncorrected — no ledger for this period, so the old whole-window divisor
    availability: str = "uncorrected"

    def __post_init__(self) -> None:
        if not self.span_weeks:
            self.span_weeks = self.weeks

    @property
    def weekly(self) -> float:
        return self.units / self.weeks if self.weeks else 0.0

    @property
    def stockout_weeks(self) -> float:
        """Time in the window the item could not be sold. Zero when unknown,
        which is not the same as zero stockouts and is labelled as such."""
        return max(0.0, self.span_weeks - self.weeks)

    def as_dict(self) -> dict:
        return {
            "weekly": round(self.weekly, 1),
            "units": self.units,
            "weeks": round(self.weeks, 1),
            "orders": self.orders,
            "span_weeks": round(self.span_weeks, 1),
            "stockout_weeks": round(self.stockout_weeks, 1),
            "availability": self.availability,
        }


def _lines(payload: dict) -> list[dict]:
    # The canonical event keeps the source order under its own key on some
    # sources and inlines it on others; look in both rather than assume.
    for candidate in (payload, payload.get("order") or {}):
        lines = candidate.get("line_items")
        if isinstance(lines, list):
            return lines
    return []


def _sellable_weeks(
    readings: list[StockLevel], start: datetime, end: datetime
) -> float | None:
    """Weeks between start and end during which the item had stock.

    Each reading is taken to hold until the next one contradicts it, which is
    what a level actually is: nobody sends a message when nothing changes. The
    reading in force at the start of the period is therefore the last one taken
    at or before it, not the first one inside it.

    Returns None when the ledger says nothing about the beginning of the period.
    Assuming an item was in stock before anybody recorded it is the same mistake
    in the other direction, and it would be invisible.
    """
    if end <= start:
        return None

    before = [r for r in readings if r.recorded_at <= start]
    inside = sorted(
        (r for r in readings if start < r.recorded_at < end), key=lambda r: r.recorded_at
    )
    if not before:
        return None

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
    return sellable / (7 * 24 * 3600)


async def weekly_demand(
    session: AsyncSession, *, now: datetime, window_weeks: float | None = None
) -> dict[str, Demand]:
    """Units sold per week per SKU across the trailing window.

    The payload is read in Python rather than with a JSON query. Both dialects
    this runs on can index into JSON, but with different operators, and the
    volume here is one query over recent orders — buying a dialect split for it
    would be paying real complexity for no measurable return.
    """
    settings = get_settings()
    window = window_weeks if window_weeks is not None else settings.demand_window_weeks
    cutoff = now - timedelta(weeks=window)

    events = list(
        await session.scalars(
            select(Event)
            .where(Event.name == "order_paid", Event.occurred_at >= cutoff)
            .order_by(Event.occurred_at)
        )
    )
    if not events:
        return {}

    # Divide by the trading history actually available, not by the width of the
    # window. A shop four weeks old divided by eight would read as half the
    # demand it has, and under-buying is the failure mode that empties shelves.
    earliest = min(e.occurred_at for e in events)
    if earliest.tzinfo is None:
        earliest = earliest.replace(tzinfo=now.tzinfo)
    observed = (now - earliest).total_seconds() / (7 * 24 * 3600)
    weeks = min(max(observed, 1.0), window)

    out: dict[str, Demand] = {}
    for event in events:
        counted: set[str] = set()
        for line in _lines(event.payload or {}):
            sku = (line.get("sku") or "").strip()
            if not sku:
                # A line with no SKU is not an error — gift cards and shipping
                # arrive this way — but it cannot be attributed to stock.
                continue
            try:
                quantity = int(line.get("quantity") or 1)
            except (TypeError, ValueError):
                quantity = 1
            entry = out.get(sku)
            if entry is None:
                entry = out[sku] = Demand(
                    sku=sku, units=0, weeks=weeks, orders=0, span_weeks=weeks
                )
            entry.units += max(quantity, 0)
            # One basket holding the same SKU on two lines is one order for it.
            if sku not in counted:
                entry.orders += 1
                counted.add(sku)
    if not out:
        return out

    # Now take the empty shelf out of the divisor. The period is the same one
    # the sales were counted over, so the two halves of the ratio describe the
    # same stretch of time.
    span_start = max(cutoff, now - timedelta(weeks=weeks))
    ledger: dict[str, list[StockLevel]] = {}
    for reading in await session.scalars(
        select(StockLevel).where(StockLevel.sku.in_(list(out)))
    ):
        recorded = reading.recorded_at
        if recorded.tzinfo is None:
            reading.recorded_at = recorded.replace(tzinfo=now.tzinfo)
        ledger.setdefault(reading.sku, []).append(reading)

    for sku, demand in out.items():
        sellable = _sellable_weeks(ledger.get(sku, []), span_start, now)
        if sellable is None:
            # Nothing recorded before this period began. The old behaviour, said
            # out loud rather than passed off as a correction.
            continue
        if sellable < MIN_AVAILABLE_WEEKS:
            demand.weeks = MIN_AVAILABLE_WEEKS
            demand.availability = "brief"
        else:
            demand.weeks = sellable
            demand.availability = "measured"
    return out
