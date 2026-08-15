"""What the forecast can tell a buyer who is setting thresholds by hand.

The per-item policy page asks for four numbers and shows none of the facts they
depend on. "Buy up to 8 weeks" is set against a demand rate that is not on the
page, for an item whose supplier may quote six weeks, with a history that may be
three weeks long, for a line the model may or may not be any good at. Every one
of those is knowable and none of it was visible.

Nothing here sets anything. It is all read-only, and deliberately so: the four
thresholds are judgements a buyer owns, and a page that quietly filled them in
would be making the judgement while appearing to ask for it. What this does is
put the evidence next to the box.

One number is a recommendation rather than a fact, and is labelled as such: the
reorder point cannot honestly be shorter than the supplier's lead time. An order
triggered four weeks out from a mill that takes six arrives two weeks after the
shelf emptied, every single time, and no forecast improvement can rescue it.
"""

from dataclasses import dataclass
from datetime import date
from statistics import mean, pstdev

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forecast.panel import ItemWeek, Panel
from sca.models import Item, StockSnapshot, Supplier, SupplierItem

# Windows offered for "measure over". Anything shorter than four weeks is noise;
# anything past half a year stops being this season.
WINDOWS = (4, 8, 13, 26)

# Where a line stops being steady. The ratio of week-to-week spread to the mean:
# below the first it is predictable, above the second it arrives in lumps and a
# cover target set on the average will be wrong in both directions.
STEADY = 0.5
LUMPY = 1.0

# Below this there is not enough history for a window comparison to mean
# anything, and picking a "best" one from four numbers would be reading noise.
MIN_WEEKS_FOR_WINDOW = 8


@dataclass
class Guidance:
    sku: str
    weekly: float | None = None
    weekly_source: str = "none"
    weeks_cover: float | None = None
    weeks_history: int = 0
    # The mill's own clock, in weeks. The reorder point is measured in cover, and
    # cover shorter than the lead time is a stockout with extra steps.
    lead_time_weeks: float | None = None
    lead_time_from: str | None = None
    suggested_reorder_weeks: float | None = None
    reorder_warning: str | None = None
    best_window: int | None = None
    best_window_error: float | None = None
    volatility: float | None = None
    steadiness: str = "unknown"
    # Whether the model actually beat the trailing average on *this* line. It wins
    # some and loses others, and one figure for the catalogue hides that.
    confidence: str = "unknown"
    confidence_detail: str | None = None

    def as_dict(self) -> dict:
        return {
            "sku": self.sku,
            "weekly": None if self.weekly is None else round(self.weekly, 2),
            "weekly_source": self.weekly_source,
            "weeks_cover": None if self.weeks_cover is None else round(self.weeks_cover, 1),
            "weeks_history": self.weeks_history,
            "lead_time_weeks": (
                None if self.lead_time_weeks is None else round(self.lead_time_weeks, 1)
            ),
            "lead_time_from": self.lead_time_from,
            "suggested_reorder_weeks": self.suggested_reorder_weeks,
            "reorder_warning": self.reorder_warning,
            "best_window": self.best_window,
            "best_window_error": (
                None if self.best_window_error is None else round(self.best_window_error, 4)
            ),
            "volatility": None if self.volatility is None else round(self.volatility, 2),
            "steadiness": self.steadiness,
            "confidence": self.confidence,
            "confidence_detail": self.confidence_detail,
        }


def _known(rows: list[ItemWeek]) -> list[ItemWeek]:
    """Weeks whose demand is knowable. A week with nothing on the shelf and
    nothing sold says nothing about what customers wanted, and letting it into a
    spread or an average drags both towards zero."""
    return [r for r in rows if not r.unknowable]


def _window_error(rows: list[ItemWeek], window: int) -> float | None:
    """How wrong a trailing average of this width would have been.

    Walked forward one week at a time, each week predicted only from the weeks
    before it — the same rule the gate follows, because a window chosen by
    looking at the weeks it is scored on is not a choice, it is a fit.
    """
    usable = _known(rows)
    if len(usable) < window + 2:
        return None
    errors, total = 0.0, 0.0
    for index in range(window, len(usable)):
        history = usable[index - window:index]
        # Divided by the weeks it could actually be sold, as the live estimator
        # does — comparing against a weaker version of it would make every window
        # look better than it is.
        days = [r.sellable_days for r in history]
        if all(d is not None for d in days):
            weeks = max(sum(days) / 7.0, 0.5)
        else:
            weeks = float(len(history))
        predicted = sum(r.units for r in history) / weeks if weeks else 0.0
        actual = float(usable[index].units)
        errors += abs(predicted - actual)
        total += actual
    return (errors / total) if total > 0 else None


def _steadiness(rows: list[ItemWeek]) -> tuple[float | None, str]:
    usable = [float(r.units) for r in _known(rows)]
    if len(usable) < 4:
        return None, "unknown"
    average = mean(usable)
    if average <= 0:
        return None, "unknown"
    ratio = pstdev(usable) / average
    if ratio < STEADY:
        return ratio, "steady"
    if ratio < LUMPY:
        return ratio, "variable"
    return ratio, "lumpy"


async def build(
    session: AsyncSession, panel: Panel, *, last_run_per_item: dict | None = None
) -> dict[str, Guidance]:
    """Everything the forecast knows that bears on a threshold, per item."""
    per_item = last_run_per_item or {}
    items = {i.sku: i for i in await session.scalars(select(Item))}
    stock = {s.sku: s for s in await session.scalars(select(StockSnapshot))}
    suppliers = {s.id: s for s in await session.scalars(select(Supplier))}
    links: dict[str, list[SupplierItem]] = {}
    for link in await session.scalars(select(SupplierItem).where(SupplierItem.active.is_(True))):
        links.setdefault(link.sku, []).append(link)

    out: dict[str, Guidance] = {}
    for sku, item in items.items():
        rows = panel.items.get(sku, [])
        guidance = Guidance(sku=sku, weeks_history=len(_known(rows)))

        snapshot = stock.get(sku)
        weekly = float(snapshot.weekly_forecast) if snapshot else 0.0
        if weekly > 0:
            guidance.weekly = weekly
            guidance.weekly_source = "forecast"
            available = (snapshot.on_hand + snapshot.on_order) if snapshot else 0
            guidance.weeks_cover = available / weekly

        # The clock that actually governs the reorder point. The item's own
        # supplier first, because that is who would be sent the order; whoever
        # else can make it is a fallback rather than the plan.
        days, source = _lead_time(item, links.get(sku, []), suppliers)
        if days:
            guidance.lead_time_weeks = days / 7.0
            guidance.lead_time_from = source
            # Rounded up to the half week, then a week of slack. An order placed
            # exactly one lead time out arrives the day the shelf empties, which
            # is on time only if nothing goes wrong and nothing ever does.
            guidance.suggested_reorder_weeks = round(days / 7.0 + 1.0, 1)
            current = float(
                item.reorder_cover_weeks if item.reorder_cover_weeks is not None else 0
            )
            if current and current < days / 7.0:
                guidance.reorder_warning = (
                    f"triggers at {current:g} weeks but {source} takes "
                    f"{days / 7.0:.1f} — every order arrives after the shelf is empty"
                )

        if len(_known(rows)) >= MIN_WEEKS_FOR_WINDOW:
            scored = [(w, _window_error(rows, w)) for w in WINDOWS]
            scored = [(w, e) for w, e in scored if e is not None]
            if scored:
                best = min(scored, key=lambda pair: pair[1])
                guidance.best_window, guidance.best_window_error = best[0], best[1]

        guidance.volatility, guidance.steadiness = _steadiness(rows)

        scores = per_item.get(sku)
        if scores:
            model = (scores.get("model") or {}).get("wape")
            baseline = (scores.get("baseline") or {}).get("wape")
            if model is None or baseline is None:
                guidance.confidence = "unscored"
                guidance.confidence_detail = "nothing sold in the weeks the model was tested on"
            elif model < baseline:
                guidance.confidence = "model"
                guidance.confidence_detail = (
                    f"the model was closer than the average here — {model:.0%} against "
                    f"{baseline:.0%}"
                )
            else:
                guidance.confidence = "average"
                guidance.confidence_detail = (
                    f"the average was closer on this line — {baseline:.0%} against "
                    f"the model's {model:.0%}"
                )
        elif guidance.weeks_history:
            guidance.confidence = "new"
            guidance.confidence_detail = "no scored run covers this line yet"

        out[sku] = guidance
    return out


def _lead_time(
    item: Item, links: list[SupplierItem], suppliers: dict[str, Supplier]
) -> tuple[int | None, str | None]:
    own = suppliers.get(item.supplier_id)
    for link in links:
        if link.supplier_id == item.supplier_id and link.lead_time_days:
            return link.lead_time_days, (own.name if own else "their supplier")
    if own and own.lead_time_days:
        return own.lead_time_days, own.name
    # Nobody named on the item. The fastest of whoever can make it, said out loud
    # as the best case rather than presented as the plan.
    options = [
        (link.lead_time_days or (suppliers[link.supplier_id].lead_time_days
                                 if link.supplier_id in suppliers else None), link)
        for link in links
    ]
    options = [(days, link) for days, link in options if days]
    if not options:
        return None, None
    days, link = min(options, key=lambda pair: pair[0])
    supplier = suppliers.get(link.supplier_id)
    return days, f"{supplier.name if supplier else 'the fastest supplier'} (fastest)"


def week_span(panel: Panel) -> tuple[date | None, date | None]:
    return panel.first_week, panel.last_week
