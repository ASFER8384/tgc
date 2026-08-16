"""The order plan, and the calendar it rests on.

What is pinned here is mostly the ways a seasonal forecast goes quietly wrong: a
peak measured against the wrong calendar, a month treated as one thing when it
holds four, an incomplete month counted as a whole one, and a level that lags a
growing business without ever looking incorrect.
"""

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select

from cdp.models.event import Event
from cdp.models.person import Person
from forecast import calendar as cal
from forecast.plan import _holt, _month_weight, _regime_multipliers, build_plan
from sca.models import Item, StockAtLocation, StockAtVariant, StockLocation, StockSnapshot

SPAN = {"first_year": 2024, "last_year": 2027}


def test_ramadan_is_found_from_the_eid_date_not_the_month():
    """The whole reason this module exists. Eid al-Fitr moves about eleven days
    earlier each Gregorian year, so the buying peak is in March one year and
    February the next — and an index built on month names is wrong in both."""
    # 2026-03-19 is Eid al-Fitr, so the ten days before it are the peak.
    assert cal.regime_of(date(2026, 3, 15), **SPAN) == cal.RAMADAN_PEAK
    assert cal.regime_of(date(2026, 3, 19), **SPAN) == cal.EID_FITR
    # The same calendar date a year later is a different part of the year,
    # because Eid has moved to 9 March. This is the assertion a month-based
    # index cannot satisfy: mid-March is the peak one year and past it the next.
    assert cal.regime_of(date(2027, 3, 15), **SPAN) == cal.POST_EID
    # And in 2025 Eid was the 30th, so the same date is early Ramadan — the
    # window has slid across the month in three consecutive years.
    assert cal.regime_of(date(2025, 3, 15), **SPAN) == cal.RAMADAN_EARLY


def test_a_month_is_not_one_season():
    """March 2026 holds the end of Ramadan, Eid itself and the fortnight after.
    Its expected trade is those added together, which is precisely what a
    month-level multiplier cannot express."""
    counts = cal.regimes_for_month(2026, 3, **SPAN)
    assert counts[cal.RAMADAN_PEAK] == 10
    assert counts[cal.EID_FITR] == 5
    assert counts[cal.POST_EID] == 8
    assert sum(counts.values()) == 31


def test_next_february_already_holds_ramadan():
    """The drift, one year on. February 2027 is mostly Ramadan and a plan built
    on month names would order for it as an ordinary February."""
    counts = cal.regimes_for_month(2027, 2, **SPAN)
    assert counts[cal.RAMADAN_EARLY] == 19


def test_a_multiplier_is_per_day_not_per_period():
    """Ten days of Eid against sixty of summer. Comparing totals would call
    summer the busier season, which is true and useless."""
    units = {cal.EID_FITR: 100, cal.SUMMER: 300}
    days = {cal.EID_FITR: 10, cal.SUMMER: 60}
    out = _regime_multipliers(units, days)
    assert out[cal.EID_FITR] > out[cal.SUMMER]


def test_a_thin_regime_is_shrunk_toward_ordinary():
    """Two years gives two Eids. One freak Eid must not set next year's order."""
    # Five times the ordinary rate in both, seen for 2 days against 50.
    thin = _regime_multipliers({"a": 2 * 5, "b": 1000}, {"a": 2, "b": 1000})
    fat = _regime_multipliers({"a": 50 * 5, "b": 1000}, {"a": 50, "b": 1000})
    assert abs(thin["a"] - 1) < abs(fat["a"] - 1)
    assert thin["a"] < 2.0


def test_the_level_follows_a_growing_series():
    """A flat mean of a growing business lags it by half the window, and a
    forecast that is quietly light does not read as wrong — it reads as a shop
    that keeps running out."""
    rising = [10, 12, 14, 16, 18, 20]
    assert _holt(rising) > sum(rising) / len(rising)
    # And it does not run away: damping keeps one month ahead near the last
    # observation rather than extrapolating a straight line forever.
    assert _holt(rising) < 26


def test_month_weight_adds_its_days_up():
    multipliers = {cal.NORMAL: 1.0, cal.NATIONAL_DAY: 2.0}
    weight = _month_weight(2026, 9, multipliers, (2024, 2027))
    # 24 ordinary days at 1.0 and 6 National Day days at 2.0.
    assert weight == pytest.approx(24 * 1.0 + 6 * 2.0)


@pytest.fixture
async def trading(client, session):
    """One item sold in two shops, every day for a year, in two sizes."""
    from sca.models import Supplier

    supplier = Supplier(code="ALN", name="Aleena Atelier")
    session.add(supplier)
    # A named shopper, so the plan has somebody to attribute the trade to.
    shopper = Person(display_name="Noura")
    session.add(shopper)
    await session.flush()
    session.add(Item(sku="ALN-ABAYA-01", name="Abaya", supplier_id=supplier.id, brand="aleena"))
    session.add(StockSnapshot(sku="ALN-ABAYA-01", on_hand=20))
    session.add_all([
        StockLocation(code="riyadh", name="Riyadh shop", kind="retail"),
        StockLocation(code="jeddah", name="Jeddah shop", kind="retail"),
    ])
    session.add_all([
        StockAtLocation(sku="ALN-ABAYA-01", location_code="riyadh", on_hand=12),
        StockAtLocation(sku="ALN-ABAYA-01", location_code="jeddah", on_hand=8),
        StockAtVariant(sku="ALN-ABAYA-01", location_code="riyadh", variant="54", on_hand=12),
        StockAtVariant(sku="ALN-ABAYA-01", location_code="jeddah", variant="54", on_hand=8),
    ])
    start = datetime(2025, 9, 1, 12, tzinfo=UTC)
    for day in range(360):
        when = start + timedelta(days=day)
        # Riyadh takes two thirds of the trade and size 54 three quarters of it.
        for where, units in (("riyadh", 2), ("jeddah", 1)):
            session.add(Event(
                source="pos", name="order_paid", occurred_at=when,
                channel="retail", person_id=shopper.id,
                payload={
                    "location": where,
                    "line_items": [
                        {"sku": "ALN-ABAYA-01", "quantity": units * 3,
                         "variant_title": "54", "price": "400.00"},
                        {"sku": "ALN-ABAYA-01", "quantity": units,
                         "variant_title": "56", "price": "400.00"},
                    ],
                },
            ))
    await session.commit()
    return supplier


async def test_the_plan_splits_by_shop_and_size(session, trading):
    plan = await build_plan(session, now=datetime(2026, 8, 16, tzinfo=UTC))
    item = plan["items"][0]

    assert plan["month"] == "2026-09"
    assert item["expected"] > 0

    shops = {loc["code"]: loc for loc in item["locations"]}
    # Riyadh sold twice what Jeddah did, so it carries about twice the share.
    assert shops["riyadh"]["share"] == pytest.approx(2 / 3, abs=0.05)
    assert shops["jeddah"]["share"] == pytest.approx(1 / 3, abs=0.05)
    # Every shop's expectation adds back to the item's.
    assert sum(loc["expected"] for loc in item["locations"]) == pytest.approx(
        item["expected"], abs=0.5
    )
    # And the sizes split three to one, as they were sold.
    sizes = {s["size"]: s for s in shops["riyadh"]["sizes"]}
    assert sizes["54"]["share"] == pytest.approx(0.75, abs=0.05)


async def test_the_order_is_what_is_missing_not_what_will_sell(session, trading):
    """The number a mill is sent is demand minus the shelf, per shop and per
    size. An item with enough of a size already is not ordered again."""
    plan = await build_plan(session, now=datetime(2026, 8, 16, tzinfo=UTC))
    item = plan["items"][0]
    assert item["order"] == sum(loc["order"] for loc in item["locations"])
    for loc in item["locations"]:
        for size in loc["sizes"]:
            assert size["order"] >= 0
            if size["on_hand"] >= size["expected"]:
                assert size["order"] == 0


async def test_the_incomplete_month_is_never_planned_from(session, trading):
    """Today is the 16th. Counting August as a whole month would halve the level
    and under-order every line in the plan."""
    plan = await build_plan(session, now=datetime(2026, 8, 16, tzinfo=UTC))
    months = [row["month"] for row in plan["items"][0]["monthly_history"]]
    assert "2026-08" not in months
    assert "2026-07" in months


async def test_a_line_with_no_history_is_not_ordered_against_a_guess(session, trading):
    from sca.models import Supplier

    supplier = await session.scalar(select(Supplier))
    session.add(Item(sku="NEW-THING-01", name="New", supplier_id=supplier.id, brand="aleena"))
    await session.commit()

    plan = await build_plan(session, now=datetime(2026, 8, 16, tzinfo=UTC))
    fresh = next(i for i in plan["items"] if i["sku"] == "NEW-THING-01")
    assert fresh["confidence"] == "no history"
    assert fresh["order"] == 0
    assert fresh["notes"]
