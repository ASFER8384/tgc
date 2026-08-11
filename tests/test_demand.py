"""Demand measured from customer orders, and the planner's use of it.

These exist because "the forecast comes from actual sales" is a claim made to
someone spending money on it. Every rule that claim depends on — the window, the
divisor, a typed forecast winning — is pinned here.
"""

from datetime import UTC, datetime, timedelta

import pytest

from cdp.models.event import Event
from cdp.models.person import Person
from sca.models import Item, StockSnapshot, Supplier
from sca.planning.demand import weekly_demand
from sca.planning.service import PlanningService

NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)


async def _person(session) -> str:
    person = Person(display_name="Demo Buyer")
    session.add(person)
    await session.flush()
    return person.id


async def _sale(session, person_id: str, *, sku: str, quantity: int, days_ago: float) -> None:
    session.add(
        Event(
            person_id=person_id,
            source="shopify",
            name="order_paid",
            occurred_at=NOW - timedelta(days=days_ago),
            payload={"line_items": [{"sku": sku, "quantity": quantity, "price": "100.00"}]},
        )
    )
    await session.flush()


async def test_averages_units_over_the_span_actually_traded(session):
    person = await _person(session)
    for days in (28, 14, 7):
        await _sale(session, person, sku="ALN-SILK-NVY", quantity=10, days_ago=days)

    demand = await weekly_demand(session, now=NOW)

    entry = demand["ALN-SILK-NVY"]
    assert entry.units == 30
    assert entry.orders == 3
    # Four weeks of history, not the eight-week window: dividing by the window
    # would report half the real demand and under-buy.
    assert entry.weeks == pytest.approx(4.0, abs=0.01)
    assert entry.weekly == pytest.approx(7.5, abs=0.01)


async def test_sales_older_than_the_window_do_not_vote(session):
    person = await _person(session)
    await _sale(session, person, sku="ALN-SILK-NVY", quantity=500, days_ago=120)

    assert await weekly_demand(session, now=NOW) == {}


async def test_a_short_history_is_never_divided_by_less_than_a_week(session):
    """Two days of trading must not be extrapolated into a huge weekly rate."""
    person = await _person(session)
    await _sale(session, person, sku="ALN-SILK-NVY", quantity=14, days_ago=2)

    entry = (await weekly_demand(session, now=NOW))["ALN-SILK-NVY"]
    assert entry.weeks == 1.0
    assert entry.weekly == pytest.approx(14.0)


async def test_lines_without_a_sku_are_skipped_not_guessed(session):
    person = await _person(session)
    session.add(
        Event(
            person_id=person,
            source="shopify",
            name="order_paid",
            occurred_at=NOW - timedelta(days=7),
            payload={"line_items": [{"quantity": 3, "title": "Gift card"}]},
        )
    )
    await session.flush()

    assert await weekly_demand(session, now=NOW) == {}


async def test_unpaid_orders_are_not_demand(session):
    person = await _person(session)
    session.add(
        Event(
            person_id=person,
            source="shopify",
            name="checkout_started",
            occurred_at=NOW - timedelta(days=7),
            payload={"line_items": [{"sku": "ALN-SILK-NVY", "quantity": 40}]},
        )
    )
    await session.flush()

    assert await weekly_demand(session, now=NOW) == {}


async def _catalogue(session, *, weekly_forecast: int) -> None:
    supplier = Supplier(code="SUP1", name="Mill", email="mill@example.com", lead_time_days=21)
    session.add(supplier)
    await session.flush()
    session.add(Item(sku="ALN-SILK-NVY", name="Navy silk", supplier_id=supplier.id, unit_cost=40))
    session.add(
        StockSnapshot(
            sku="ALN-SILK-NVY", on_hand=20, on_order=0, weekly_forecast=weekly_forecast
        )
    )
    await session.flush()


async def test_planner_uses_measured_demand_when_no_forecast_was_typed(session):
    await _catalogue(session, weekly_forecast=0)
    person = await _person(session)
    for days in (28, 14, 7):
        await _sale(session, person, sku="ALN-SILK-NVY", quantity=10, days_ago=days)

    suggestions = await PlanningService(session).suggest(now=NOW)

    assert len(suggestions) == 1
    line = suggestions[0]
    assert line.forecast_source == "sales"
    assert line.weekly_demand == pytest.approx(7.5, abs=0.01)
    # The reason a buyer reads has to say where the number came from.
    assert "30 sold in 4 weeks" in line.reason


async def test_a_typed_forecast_beats_the_measured_one(session):
    """Someone who sets a forecast knows about a launch the past cannot see."""
    await _catalogue(session, weekly_forecast=90)
    person = await _person(session)
    await _sale(session, person, sku="ALN-SILK-NVY", quantity=10, days_ago=7)

    line = (await PlanningService(session).suggest(now=NOW))[0]

    assert line.forecast_source == "manual"
    assert line.weekly_demand == pytest.approx(90.0)
    # Still measured and reported alongside, so a stale typed figure is visible.
    assert line.observed is not None
    assert line.observed.units == 10


async def test_no_forecast_and_no_sales_stays_silent(session):
    await _catalogue(session, weekly_forecast=0)

    assert await PlanningService(session).suggest(now=NOW) == []
