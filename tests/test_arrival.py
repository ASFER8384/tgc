"""If we order today, when does it arrive.

The estimate is a sum of parts with very different standing, and the thing worth
protecting is that the weakest part cannot borrow authority from the strongest.
So these tests care less about the total than about whether each leg is labelled
truthfully — measured where it was measured, assumed where it was assumed.
"""

from datetime import UTC, datetime, timedelta

import pytest

from sca.analytics.arrival import MODES, ArrivalService
from sca.analytics.geo import distance_km, haversine, origin
from sca.models import AuditLog, PurchaseOrder, Supplier

# A Wednesday morning in Riyadh, inside Chinese working hours.
WEDNESDAY = datetime(2026, 9, 2, 6, 0, tzinfo=UTC)


def _supplier(**over) -> Supplier:
    base = dict(
        code="GZ-TEX", name="Guangzhou Silk Mill", country="CN",
        email="orders@gzsilkmill.cn", timezone="Asia/Shanghai",
        working_days="1,2,3,4,5", work_start_hour=9, work_end_hour=17,
        lead_time_days=21, currency="CNY",
    )
    return Supplier(**(base | over))


# --------------------------------------------------------------------- geometry
def test_distance_is_computed_not_guessed():
    """Riyadh to Guangzhou is about 6,700km, which is checkable on any map."""
    assert 6500 < distance_km("CN") < 7000
    assert 2200 < distance_km("TR") < 2700


def test_an_unknown_country_reports_no_distance_rather_than_a_default():
    """A default would put a journey to nowhere into the sum and hide the fact
    that nobody has said where this supplier is."""
    assert distance_km("ZZ") is None
    assert distance_km(None) is None
    assert origin("ZZ") is None


def test_the_same_point_is_zero_kilometres_away():
    assert haversine((24.7136, 46.6753), (24.7136, 46.6753)) == 0


def test_transit_assumptions_match_published_lane_times():
    """Sea from China lands near a month and air near a week. If a change here
    moves either outside the range forwarders actually quote, it is wrong."""
    km = distance_km("CN")
    sea_base, sea_rate = MODES["sea"]
    air_base, air_rate = MODES["air"]
    assert 25 <= sea_base + sea_rate * km / 1000 <= 35
    assert 5 <= air_base + air_rate * km / 1000 <= 10


# ----------------------------------------------------------------------- legs
@pytest.mark.asyncio
async def test_without_history_the_production_leg_is_quoted_not_measured(session):
    supplier = _supplier()
    session.add(supplier)
    await session.flush()

    out = await ArrivalService(session).estimate(supplier, now=WEDNESDAY)
    making = next(leg for leg in out.legs if leg.name.startswith("They make"))
    assert making.basis == "quoted"
    assert making.days == 21
    assert out.orders_measured == 0


@pytest.mark.asyncio
async def test_with_history_the_production_leg_is_measured_from_real_receipts(session):
    """And it reports the supplier's own figure beside it, because a mill that
    quotes 21 and takes 30 is the single most useful thing this can surface."""
    supplier = _supplier()
    session.add(supplier)
    await session.flush()

    sent = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)
    for number, days in (("PO-1", 28), ("PO-2", 32)):
        order = PurchaseOrder(
            number=number, supplier_id=supplier.id, status="received",
            total_value=1000, sent_at=sent,
        )
        session.add(order)
        await session.flush()
        session.add(
            AuditLog(
                actor="buyer", action="order.receive", entity="purchase_order",
                entity_id=order.id, meta={}, created_at=sent + timedelta(days=days),
            )
        )
    await session.flush()

    out = await ArrivalService(session).estimate(supplier, now=WEDNESDAY)
    making = next(leg for leg in out.legs if leg.name.startswith("They make"))
    assert making.basis == "measured"
    assert making.days == 30.0
    assert out.orders_measured == 2
    assert "21" in making.detail


@pytest.mark.asyncio
async def test_a_receipt_before_the_send_is_ignored(session):
    """Backfilled demo rows travel backwards in time. Averaging one in would
    quietly shorten every estimate for that supplier."""
    supplier = _supplier()
    session.add(supplier)
    await session.flush()
    sent = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)
    order = PurchaseOrder(
        number="PO-1", supplier_id=supplier.id, status="received",
        total_value=1000, sent_at=sent,
    )
    session.add(order)
    await session.flush()
    session.add(
        AuditLog(
            actor="buyer", action="order.receive", entity="purchase_order",
            entity_id=order.id, meta={}, created_at=sent - timedelta(days=2),
        )
    )
    await session.flush()

    out = await ArrivalService(session).estimate(supplier, now=WEDNESDAY)
    making = next(leg for leg in out.legs if leg.name.startswith("They make"))
    assert making.basis == "quoted"


@pytest.mark.asyncio
async def test_a_same_hour_receipt_is_not_treated_as_a_measurement(session):
    """Found on live data: five orders received minutes after being sent, during
    testing, reported as a measured lead time of zero days. Nothing crosses a
    border in an hour, and a fabricated zero is worse than an honest quote
    because it carries the word "measured"."""
    supplier = _supplier()
    session.add(supplier)
    await session.flush()
    sent = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)
    order = PurchaseOrder(
        number="PO-1", supplier_id=supplier.id, status="received",
        total_value=1000, sent_at=sent,
    )
    session.add(order)
    await session.flush()
    session.add(
        AuditLog(
            actor="buyer", action="order.receive", entity="purchase_order",
            entity_id=order.id, meta={}, created_at=sent + timedelta(minutes=20),
        )
    )
    await session.flush()

    out = await ArrivalService(session).estimate(supplier, now=WEDNESDAY)
    making = next(leg for leg in out.legs if leg.name.startswith("They make"))
    assert making.basis == "quoted"
    assert making.days == 21
    assert out.orders_measured == 0


@pytest.mark.asyncio
async def test_the_transit_leg_says_it_is_assumed(session):
    """It is the largest term in the sum and the one nobody has verified. If it
    ever stops saying so, the whole estimate starts overclaiming."""
    supplier = _supplier()
    session.add(supplier)
    await session.flush()

    out = await ArrivalService(session).estimate(supplier, mode="sea", now=WEDNESDAY)
    transit = next(leg for leg in out.legs if leg.name.startswith("In transit"))
    assert transit.basis == "assumed"
    assert "not tracked" in transit.detail


@pytest.mark.asyncio
async def test_chinese_new_year_is_counted_against_a_february_order(session):
    """The reason the holiday calendar earns its dependency. A mill ordered from
    in late January is shut for a fortnight, and it is knowable a year ahead."""
    supplier = _supplier()
    session.add(supplier)
    await session.flush()

    january = datetime(2027, 1, 25, 6, 0, tzinfo=UTC)
    out = await ArrivalService(session).estimate(supplier, now=january)
    closed = [leg for leg in out.legs if leg.name == "Closed at origin"]
    assert closed, "Spring Festival should fall inside a 21 day window from 25 January"
    assert closed[0].days >= 4
    assert closed[0].basis == "calendar"


@pytest.mark.asyncio
async def test_a_summer_order_from_china_is_not_charged_for_new_year(session):
    supplier = _supplier()
    session.add(supplier)
    await session.flush()

    out = await ArrivalService(session).estimate(supplier, now=datetime(2027, 7, 1, 6, tzinfo=UTC))
    closed = [leg for leg in out.legs if leg.name == "Closed at origin"]
    assert closed == []


@pytest.mark.asyncio
async def test_air_lands_sooner_than_sea_from_the_same_place(session):
    supplier = _supplier()
    session.add(supplier)
    await session.flush()

    air = await ArrivalService(session).estimate(supplier, mode="air", now=WEDNESDAY)
    sea = await ArrivalService(session).estimate(supplier, mode="sea", now=WEDNESDAY)
    assert air.total_days < sea.total_days
    assert air.arrives_on < sea.arrives_on


@pytest.mark.asyncio
async def test_a_supplier_with_no_country_still_estimates_the_rest(session):
    """Losing the distance should cost the transit leg and nothing else — the
    production time and their working hours are still known.

    Nothing is placed by a default: the timezone here maps to no country, which
    is the only way the origin genuinely cannot be worked out.
    """
    supplier = _supplier(country=None, timezone="UTC")
    session.add(supplier)
    await session.flush()

    out = await ArrivalService(session).estimate(supplier, now=WEDNESDAY)
    assert out.distance_km is None
    transit = next(leg for leg in out.legs if leg.name.startswith("In transit"))
    assert transit.days == 0.0
    assert "country not set" in transit.detail
    assert out.total_days > 20


@pytest.mark.asyncio
async def test_a_country_inferred_from_the_timezone_says_that_it_was_inferred(session):
    """A mill on Asia/Shanghai is almost certainly in China, and estimating from
    Guangzhou beats refusing to estimate at all. But the whole distance rests on
    that guess, so the guess is on screen rather than behind it."""
    supplier = _supplier(country=None)
    session.add(supplier)
    await session.flush()

    out = await ArrivalService(session).estimate(supplier, now=WEDNESDAY)
    assert out.country == "CN"
    assert out.distance_km is not None
    transit = next(leg for leg in out.legs if leg.name.startswith("In transit"))
    assert transit.basis == "assumed"
    assert "inferred from their timezone" in transit.detail


@pytest.mark.asyncio
async def test_a_stored_country_is_never_overruled_by_the_timezone(session):
    """A supplier whose record says India and whose clock says Shanghai has one
    of the two wrong, and the one somebody typed wins — silently preferring the
    timezone would make the field unfixable from the console."""
    supplier = _supplier(country="IN", timezone="Asia/Shanghai")
    session.add(supplier)
    await session.flush()

    out = await ArrivalService(session).estimate(supplier, now=WEDNESDAY)
    assert out.country == "IN"
    assert out.hub == "Mumbai"
    transit = next(leg for leg in out.legs if leg.name.startswith("In transit"))
    assert "inferred" not in transit.detail


@pytest.mark.asyncio
async def test_an_order_placed_after_they_close_waits_for_their_morning(session):
    supplier = _supplier()
    session.add(supplier)
    await session.flush()

    # 15:00 UTC on a Friday is 23:00 in Shanghai — they are gone until Monday.
    friday_night = datetime(2026, 9, 4, 15, 0, tzinfo=UTC)
    out = await ArrivalService(session).estimate(supplier, now=friday_night)
    reaching = next(leg for leg in out.legs if leg.name == "Reaches them")
    assert reaching.days > 2
    assert reaching.basis == "calendar"


# ------------------------------------------------------------------- endpoint
@pytest.mark.asyncio
async def test_the_endpoint_returns_every_mode(client, supplier_payload, monkeypatch):
    # No network in the tests: the advisory is real and belongs in production,
    # but a suite that reaches the internet fails for reasons nobody caused.
    async def no_weather(country, **kwargs):
        return None

    monkeypatch.setattr("sca.api.coordination.advisory", no_weather)
    await client.post("/suppliers", json=supplier_payload | {"country": "CN"})

    result = await client.get(f"/suppliers/{supplier_payload['code']}/arrival")
    assert result.status_code == 200
    body = result.json()
    assert body["mode"] == "sea"
    assert set(body["alternatives"]) == {"air", "road"}
    assert {leg["basis"] for leg in body["legs"]} <= {"measured", "quoted", "calendar", "assumed"}


@pytest.mark.asyncio
async def test_an_unknown_mode_is_refused(client, supplier_payload, monkeypatch):
    async def no_weather(country, **kwargs):
        return None

    monkeypatch.setattr("sca.api.coordination.advisory", no_weather)
    await client.post("/suppliers", json=supplier_payload | {"country": "CN"})
    result = await client.get(f"/suppliers/{supplier_payload['code']}/arrival?mode=teleport")
    assert result.status_code == 422


@pytest.mark.asyncio
async def test_an_unknown_supplier_is_a_404(client):
    assert (await client.get("/suppliers/NOPE/arrival")).status_code == 404
