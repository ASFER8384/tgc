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
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# The supplier side reading the customer side. This is the merge doing work:
# before it, this import crossed a service boundary and a network.
from cdp.models.event import Event
from sca.config import get_settings


@dataclass
class Demand:
    """What was sold, and over how long — kept together because the average
    means nothing without the span it was taken over. Two units a week from
    eight weeks of trading is a signal; the same figure from four days is not."""

    sku: str
    units: int
    weeks: float
    orders: int

    @property
    def weekly(self) -> float:
        return self.units / self.weeks if self.weeks else 0.0

    def as_dict(self) -> dict:
        return {
            "weekly": round(self.weekly, 1),
            "units": self.units,
            "weeks": round(self.weeks, 1),
            "orders": self.orders,
        }


def _lines(payload: dict) -> list[dict]:
    # The canonical event keeps the source order under its own key on some
    # sources and inlines it on others; look in both rather than assume.
    for candidate in (payload, payload.get("order") or {}):
        lines = candidate.get("line_items")
        if isinstance(lines, list):
            return lines
    return []


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
                entry = out[sku] = Demand(sku=sku, units=0, weeks=weeks, orders=0)
            entry.units += max(quantity, 0)
            # One basket holding the same SKU on two lines is one order for it.
            if sku not in counted:
                entry.orders += 1
                counted.add(sku)
    return out
