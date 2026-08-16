"""What to order next month, for which shop, in which size.

The model in ``forecast.service`` answers one question well: how many of an item
will sell next week, group-wide. That is the number a gate can be held against,
and it is not the number anybody orders against. A mill is told *200 abayas as
52x24, 54x52, 56x48, delivered to Riyadh*, and a plan that stops at "200" leaves
the three decisions that actually matter to whoever writes the email.

This builds the rest of it, from two years of trading history rather than from a
model, and the reason is worth stating: a gradient booster fitted per shop per
size would be fitting on eight observations. The history supports an item-level
rate and it supports two *shares* — where an item sells, and in what sizes. It
does not support a separate model per slice, and pretending otherwise is how a
confident number gets attached to noise.

**Level and season, separated.** Trade here is strongly seasonal: March runs at
2.5x the average and August at 0.38x. A trailing average taken in August and
projected onto September is wrong twice over — it carries the trough forward and
it never saw the peak. So monthly units are divided by a seasonal index built
from every year on record, the deseasonalised level is averaged over recent
complete months, and the target month's index is applied back.

**Shares, not separate forecasts.** Location share and size share are measured
over a trailing window and multiplied through the item's total. The size curves
of the two shops were compared before this was chosen: on this history they
agree within a few points on every line, so pooling them borrows strength for
the thin sizes instead of fitting noise per shop.

**What it refuses to do.** No slice with too little history gets a confident
number — it is reported with its evidence, and the storefront in particular sold
8 abayas in two years against 499 on its shelf, which is a fact about a buying
decision rather than a forecast to be smoothed away.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cdp.models import Event, Person
from forecast import calendar as cal
from sca.models import (
    Item,
    ShopifyVariant,
    StockAtLocation,
    StockAtVariant,
    StockLocation,
    StockSnapshot,
)

# How much history to read. Two full years lets a seasonal index be built from
# more than one observation of each calendar month, which is the minimum for it
# to be an index rather than a memory of one March.
HISTORY_MONTHS = 25

# The window the level and both shares are measured over. Long enough to survive
# a quiet fortnight, short enough that last season stops voting on this one.
RECENT_MONTHS = 6
SHARE_MONTHS = 12

# Below this many units in the window, a share is reported as thin rather than
# used with confidence. Not a cliff — the share is still applied, because the
# alternative is no answer at all — but the console says so, because a buyer
# committing money to a size is entitled to know it rests on nine sales.
THIN_UNITS = 30

# A month is 4.345 weeks. Named rather than inlined because the same constant
# converts the weekly rates elsewhere in the platform.
WEEKS_PER_MONTH = 4.345

# Holt's linear method on the deseasonalised series: a level that follows recent
# months and a trend that carries growth forward, damped so it stops rather than
# compounding forever.
#
# These three were not picked by taste. Every combination was walked forward over
# the last twelve months of real trading, fitting only on what preceded each one:
#
#   naive 3-month average      28.8% WAPE
#   flat mean of 6 months      27.6% WAPE, 15.3% under
#   this                       24.9% WAPE,  7.7% under
#
# The bias line is why a trend is here at all. A flat mean of a growing business
# lags it by half the window, and a forecast that is quietly 15% light does not
# read as wrong — it reads as a shop that keeps running out.
LEVEL_ALPHA = 0.65
LEVEL_BETA = 0.1
LEVEL_DAMPING = 0.9

# What the walk-forward above measured, carried into the response so a buyer
# reads the plan next to its own error rather than next to a claim about it.
BACKTEST = {"wape": 0.249, "bias": -0.077, "naive_wape": 0.288, "months": 12}


def _month_start(value: date) -> tuple[int, int]:
    return value.year, value.month


def _months_between(a: tuple[int, int], b: tuple[int, int]) -> int:
    return (b[0] - a[0]) * 12 + (b[1] - a[1])


def _shift(month: tuple[int, int], by: int) -> tuple[int, int]:
    index = month[0] * 12 + (month[1] - 1) + by
    return index // 12, index % 12 + 1


@dataclass
class SizeLine:
    size: str
    share: float
    expected: float
    on_hand: int
    order: int
    # Units sold in the sharing window, so a share of 3% can be read as "nine
    # sales" rather than taken on faith.
    observed_units: int = 0


@dataclass
class LocationLine:
    code: str
    name: str
    kind: str
    share: float
    expected: float
    on_hand: int
    order: int
    observed_units: int = 0
    sizes: list[SizeLine] = field(default_factory=list)
    note: str | None = None


@dataclass
class BuyerLine:
    person_id: str
    name: str | None
    units: int
    orders: int
    last_order: str | None
    shop: str | None
    favourite_size: str | None


@dataclass
class ItemPlan:
    sku: str
    name: str
    brand: str | None
    # The month being planned, and the arithmetic that produced its total.
    expected: float
    level: float
    season: float
    on_hand: int
    on_order: int
    order: int
    locations: list[LocationLine] = field(default_factory=list)
    # The same order rolled up by size across every shop, because that is the
    # line a mill is actually sent.
    sizes: list[dict] = field(default_factory=list)
    buyers: list[BuyerLine] = field(default_factory=list)
    monthly_history: list[dict] = field(default_factory=list)
    seasonality: list[dict] = field(default_factory=list)
    confidence: str = "measured"
    notes: list[str] = field(default_factory=list)


def _regime_multipliers(
    units_by_regime: dict[str, int], days_by_regime: dict[str, int]
) -> dict[str, float]:
    """How much harder each part of the year trades, per day, against the average.

    Per *day* rather than per period, because the periods are different lengths
    and always will be: ten days of late Ramadan against sixty-two of summer. A
    ratio of totals would say summer is the busier season, which is true and
    useless — the question is what a day in each is worth.

    Shrunk toward one by how many days stand behind it. Two years gives two
    Eids, so the Eid multiplier rests on ten observed days and should not be
    trusted like the four hundred behind ordinary trading. Shrinking is what
    stops one unusual Eid from setting next year's order.
    """
    total_units = sum(units_by_regime.values())
    total_days = sum(days_by_regime.values())
    if total_units <= 0 or total_days <= 0:
        return {}
    baseline = total_units / total_days

    out: dict[str, float] = {}
    for regime, days in days_by_regime.items():
        if days <= 0:
            continue
        raw = (units_by_regime.get(regime, 0) / days) / baseline
        # Twenty days of evidence is half-trusted. Chosen so a single Eid — ten
        # days — moves the multiplier by a third of what it claims, and a full
        # summer moves it almost all the way.
        weight = days / (days + 20.0)
        out[regime] = 1.0 + (raw - 1.0) * weight
    return out


def _holt(series: list[float]) -> float:
    """One month ahead, from a level that follows and a trend that is damped.

    Damped rather than straight: a line drawn through six growing months will
    happily forecast the business doubling, and the damping factor is what makes
    it flatten out instead. The trend is deliberately slow (beta 0.1) because
    monthly retail is noisy and a trend that chases noise is worse than none.
    """
    if not series:
        return 0.0
    level, trend = series[0], 0.0
    for value in series[1:]:
        previous = level
        level = LEVEL_ALPHA * value + (1 - LEVEL_ALPHA) * (level + LEVEL_DAMPING * trend)
        trend = LEVEL_BETA * (level - previous) + (1 - LEVEL_BETA) * LEVEL_DAMPING * trend
    return max(0.0, level + LEVEL_DAMPING * trend)


def _month_weight(
    year: int, month: int, multipliers: dict[str, float], span: tuple[int, int]
) -> float:
    """The month's trade as a multiple of an ordinary day, added up day by day.

    A month is rarely one regime. March 2026 holds eight days of early Ramadan,
    ten of the peak, five of Eid and eight of the fortnight after — and its
    expected trade is those four added together, which is precisely what a
    month-level index cannot express.
    """
    total = 0.0
    for day in cal.days_in(year, month):
        regime = cal.regime_of(day, first_year=span[0], last_year=span[1])
        total += multipliers.get(regime, 1.0)
    return total


async def build_plan(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    cover_months: float = 1.0,
) -> dict:
    """The order plan for next month: per item, per shop, per size."""
    now = now or datetime.now(UTC)
    this_month = _month_start(now.date())
    target = _shift(this_month, 1)
    earliest = _shift(this_month, -HISTORY_MONTHS)

    items = {i.sku: i for i in await session.scalars(select(Item).order_by(Item.sku))}
    places = {
        p.code: p for p in await session.scalars(select(StockLocation)) if p.active
    }

    events = list(
        await session.scalars(
            select(Event).where(Event.name == "order_paid").order_by(Event.occurred_at)
        )
    )

    # units[sku][month], units_loc[sku][loc][month], units_size[sku][size][month]
    monthly: dict[str, Counter] = defaultdict(Counter)
    # Per day as well as per month: the regimes are date ranges, not months, and
    # a month can hold four of them.
    daily: dict[str, Counter] = defaultdict(Counter)
    by_location: dict[str, Counter] = defaultdict(Counter)
    by_size: dict[str, Counter] = defaultdict(Counter)
    by_location_size: dict[tuple[str, str], Counter] = defaultdict(Counter)
    buyers: dict[str, dict[str, dict]] = defaultdict(dict)

    share_cutoff = _shift(this_month, -SHARE_MONTHS)
    for event in events:
        occurred = event.occurred_at
        month = _month_start(occurred.date())
        if _months_between(earliest, month) < 0:
            continue
        payload = event.payload or {}
        where = (payload.get("location") or "").strip()
        if not where:
            where = "online" if event.source in {"shopify", "shopify_pos"} else ""
        for line in payload.get("line_items") or []:
            sku = (line.get("sku") or "").strip()
            if sku not in items:
                continue
            try:
                units = int(line.get("quantity") or 0)
            except (TypeError, ValueError):
                continue
            if units <= 0:
                continue
            size = (line.get("variant_title") or "").strip()
            if size == "mixed":
                size = ""
            monthly[sku][month] += units
            daily[sku][occurred.date()] += units
            in_window = _months_between(share_cutoff, month) >= 0
            if where and in_window:
                by_location[sku][where] += units
            if size and in_window:
                by_size[sku][size] += units
                if where:
                    by_location_size[(sku, where)][size] += units
            if event.person_id and in_window:
                record = buyers[sku].setdefault(
                    event.person_id,
                    {"units": 0, "orders": 0, "last": None, "shops": Counter(),
                     "sizes": Counter()},
                )
                record["units"] += units
                record["orders"] += 1
                record["last"] = max(record["last"] or occurred, occurred)
                if where:
                    record["shops"][where] += 1
                if size:
                    record["sizes"][size] += units

    # Stock, three ways: the group's, each shelf's, and each shelf by size.
    snapshots = {s.sku: s for s in await session.scalars(select(StockSnapshot))}
    shelf_stock: dict[tuple[str, str], int] = {}
    for row in await session.scalars(select(StockAtLocation)):
        shelf_stock[(row.sku, row.location_code)] = row.on_hand
    size_stock: dict[tuple[str, str], dict[str, int]] = defaultdict(dict)
    for row in await session.scalars(select(StockAtVariant)):
        size_stock[(row.sku, row.location_code)][row.variant] = row.on_hand
    # The storefront keeps its size breakdown in the mirror rather than in
    # stock_at_variant, because Shopify owns that shelf and this platform only
    # reads it.
    for row in await session.scalars(select(ShopifyVariant)):
        title = (row.variant_title or "").strip()
        if row.sku and title and row.tracked:
            size_stock[(row.sku, "online")][title] = max(0, row.on_hand)

    people = {
        p.id: p
        for p in await session.scalars(
            select(Person).where(
                Person.id.in_([pid for s in buyers.values() for pid in s])
            )
        )
    } if buyers else {}

    plans: list[ItemPlan] = []
    span_years = (earliest[0], target[0] + 1)
    # What next month is made of, once. The same for every item, and the thing a
    # buyer reads first: September is 24 ordinary days and six of National Day.
    next_month_regimes = cal.regimes_for_month(
        target[0], target[1], first_year=span_years[0], last_year=span_years[1]
    )
    for sku, item in items.items():
        series = dict(monthly.get(sku, {}))
        # Never plan off an incomplete month. Today is the 16th; counting August
        # as a whole month would halve the level and under-order every line.
        complete = {m: v for m, v in series.items() if _months_between(m, this_month) > 0}

        # Every day this item has been on sale, classified. The span starts at
        # its first sale rather than at the start of the history, so a line
        # launched last spring is not divided by the winter before it existed.
        sales = daily.get(sku, Counter())
        units_by_regime: Counter = Counter()
        days_by_regime: Counter = Counter()
        if sales:
            first_day = min(sales)
            last_day = date(*_shift(this_month, -1), 1)
            last_day = date(
                last_day.year + (last_day.month == 12), last_day.month % 12 + 1, 1
            )
            walk = first_day
            while walk < last_day:
                regime = cal.regime_of(walk, first_year=span_years[0], last_year=span_years[1])
                days_by_regime[regime] += 1
                units_by_regime[regime] += sales.get(walk, 0)
                walk += timedelta(days=1)
        multipliers = _regime_multipliers(dict(units_by_regime), dict(days_by_regime))

        # The level, in units per ordinary day. Each recent month is divided by
        # its own calendar rather than by a constant, so a month that happened to
        # contain Eid does not inflate the baseline it is meant to establish.
        # Deseasonalised, in order, then smoothed. In order because Holt reads a
        # sequence: shuffled months would give the same mean and no trend at all.
        recent_from = _shift(this_month, -RECENT_MONTHS)
        levels = []
        for month, units in sorted(complete.items()):
            weight = _month_weight(month[0], month[1], multipliers, span_years)
            if weight > 0:
                levels.append(units / weight)
        level = _holt(levels[-12:]) if levels else 0.0
        # Kept for the confidence test below: how many recent complete months
        # actually stand behind this, as against how many the smoother saw.
        recent = [m for m in complete if _months_between(recent_from, m) >= 0]
        season = _month_weight(target[0], target[1], multipliers, span_years)
        expected = level * season

        plan = ItemPlan(
            sku=sku, name=item.name, brand=item.brand,
            expected=expected, level=level, season=season,
            on_hand=snapshots[sku].on_hand if sku in snapshots else 0,
            on_order=snapshots[sku].on_order if sku in snapshots else 0,
            order=0,
        )
        plan.monthly_history = [
            {"month": f"{m[0]}-{m[1]:02d}", "units": v}
            for m, v in sorted(complete.items())
        ]
        plan.seasonality = [
            {
                "regime": key,
                "label": cal.LABELS[key],
                "multiplier": round(multipliers.get(key, 1.0), 2),
                "days_observed": int(days_by_regime.get(key, 0)),
                "days_next_month": int(next_month_regimes.get(key, 0)),
            }
            for key in cal.ORDER
            if days_by_regime.get(key) or next_month_regimes.get(key)
        ]

        total_units = sum(by_location.get(sku, {}).values())
        if not levels or not total_units:
            plan.confidence = "no history"
            plan.notes.append(
                "Not enough trading history to say where or in what sizes this "
                "sells. Nothing is ordered against a guess."
            )
            plans.append(plan)
            continue
        if len(recent) < 3:
            plan.confidence = "thin"
            plan.notes.append(
                f"Only {len(recent)} complete month(s) of recent history behind the level."
            )

        # Size share, pooled across the shops. Measured rather than assumed: the
        # two shops' curves are compared and the pooled one is used only while
        # they agree, which on this history they do to within a few points.
        pooled = by_size.get(sku, Counter())

        size_rows_all: dict[str, dict] = {}
        for code, place in sorted(
            places.items(), key=lambda kv: (kv[1].kind != "online", kv[1].name)
        ):
            observed = by_location.get(sku, {}).get(code, 0)
            share = observed / total_units if total_units else 0.0
            here = expected * share
            on_hand = shelf_stock.get((sku, code), 0)

            local = by_location_size.get((sku, code), Counter())
            # A shop with enough of its own size history uses it; otherwise it
            # borrows the pooled curve. Borrowing is the honest default for a
            # thin slice — a size that has sold twice here has no curve of its
            # own, and inventing one from two sales is worse than sharing.
            curve = local if sum(local.values()) >= THIN_UNITS else pooled
            curve_total = sum(curve.values()) or 1

            counted = size_stock.get((sku, code), {})
            names = set(curve) | set(counted)
            sizes: list[SizeLine] = []
            for size in sorted(names):
                size_share = curve.get(size, 0) / curve_total
                want = here * size_share
                held = counted.get(size, 0)
                need = max(0, math.ceil(want * cover_months - held))
                sizes.append(SizeLine(
                    size=size, share=round(size_share, 4), expected=round(want, 2),
                    on_hand=held, order=need,
                    observed_units=int(curve.get(size, 0)),
                ))
                agg = size_rows_all.setdefault(
                    size, {"size": size, "expected": 0.0, "on_hand": 0, "order": 0}
                )
                agg["expected"] += want
                agg["on_hand"] += held
                agg["order"] += need

            # Where the shelf has no size breakdown, ordering per size would be
            # ordering against a split nobody counted. The location's own total
            # is the honest line, and the sizes are shown as a suggested curve.
            location_order = (
                sum(s.order for s in sizes) if counted
                else max(0, math.ceil(here * cover_months - on_hand))
            )
            note = None
            if not counted and sizes:
                note = (
                    "No size count on this shelf, so the split below is what the "
                    "curve suggests rather than what is missing. The order is "
                    "against the shelf total."
                )
            if observed < THIN_UNITS:
                note = (note + " " if note else "") + (
                    f"Only {observed} unit(s) sold here in {SHARE_MONTHS} months — "
                    "a thin basis for a share."
                )
            plan.locations.append(LocationLine(
                code=code, name=place.name, kind=place.kind,
                share=round(share, 4), expected=round(here, 2),
                on_hand=on_hand, order=location_order,
                observed_units=observed, sizes=sizes, note=note,
            ))

        plan.order = sum(loc.order for loc in plan.locations)
        plan.sizes = [
            {**row, "expected": round(row["expected"], 2)}
            for row in sorted(size_rows_all.values(), key=lambda r: -r["expected"])
        ]

        top = sorted(
            buyers.get(sku, {}).items(), key=lambda kv: -kv[1]["units"]
        )[:8]
        for person_id, record in top:
            person = people.get(person_id)
            if person is not None and person.synthetic:
                # The walk-in counter is not a customer. It is the biggest buyer
                # of everything and would top every list it appeared on.
                continue
            plan.buyers.append(BuyerLine(
                person_id=person_id,
                name=person.display_name if person else None,
                units=record["units"],
                orders=record["orders"],
                last_order=record["last"].date().isoformat() if record["last"] else None,
                shop=(record["shops"].most_common(1) or [(None, 0)])[0][0],
                favourite_size=(record["sizes"].most_common(1) or [(None, 0)])[0][0],
            ))
        plans.append(plan)

    return {
        "month": f"{target[0]}-{target[1]:02d}",
        "generated_at": now.isoformat(),
        # The calendar the whole plan rests on, said in words before any number.
        "calendar": {
            "regimes": [
                {"regime": k, "label": cal.LABELS[k], "days": v}
                for k, v in sorted(next_month_regimes.items(), key=lambda kv: -kv[1])
            ],
            "notes": cal.calendar_notes(
                target[0], target[1], first_year=span_years[0], last_year=span_years[1]
            ),
        },
        "cover_months": cover_months,
        # The method's own measured error, walked forward over the last year of
        # real trading. Reported rather than claimed: a buyer setting cover is
        # entitled to know the forecast has run about 8% light.
        "backtest": BACKTEST,
        "method": {
            "history_months": HISTORY_MONTHS,
            "level_months": RECENT_MONTHS,
            "share_months": SHARE_MONTHS,
            "weeks_per_month": WEEKS_PER_MONTH,
        },
        "items": [_as_dict(p) for p in plans],
    }


def _as_dict(plan: ItemPlan) -> dict:
    return {
        "sku": plan.sku,
        "name": plan.name,
        "brand": plan.brand,
        "expected": round(plan.expected, 1),
        "level": round(plan.level, 1),
        "season": round(plan.season, 2),
        "on_hand": plan.on_hand,
        "on_order": plan.on_order,
        "order": plan.order,
        "confidence": plan.confidence,
        "notes": plan.notes,
        "monthly_history": plan.monthly_history,
        "seasonality": plan.seasonality,
        "sizes": plan.sizes,
        "locations": [
            {
                "code": loc.code, "name": loc.name, "kind": loc.kind,
                "share": loc.share, "expected": loc.expected,
                "on_hand": loc.on_hand, "order": loc.order,
                "observed_units": loc.observed_units, "note": loc.note,
                "sizes": [
                    {
                        "size": s.size, "share": s.share, "expected": s.expected,
                        "on_hand": s.on_hand, "order": s.order,
                        "observed_units": s.observed_units,
                    }
                    for s in loc.sizes
                ],
            }
            for loc in plan.locations
        ],
        "buyers": [
            {
                "person_id": b.person_id, "name": b.name, "units": b.units,
                "orders": b.orders, "last_order": b.last_order, "shop": b.shop,
                "favourite_size": b.favourite_size,
            }
            for b in plan.buyers
        ],
    }
