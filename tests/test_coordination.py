"""Measuring what the coordination cost.

The claim is that elapsed time is governed by round trips rather than by
processing speed. These tests do not check that the claim is true — that is a
property of a supplier network, not of this code. They check that the numbers
reported are the ones actually in the database, because a proof surface that
flatters the system it measures is worse than none.
"""

from datetime import UTC, datetime, timedelta

import pytest

from sca.coordination.service import CoordinationService
from sca.models import InboundMessage, Issue, PurchaseOrder, Supplier
from sca.scheduling.windows import WorkingHours, working_hours_between

# Friday 09:00 UTC, which is midday in Riyadh and a working morning in Shanghai.
FRIDAY = datetime(2026, 8, 14, 9, 0, tzinfo=UTC)


def _supplier(**over) -> Supplier:
    base = dict(
        code="GZ-TEX", name="Guangzhou Silk Mill", email="orders@gzsilkmill.cn",
        timezone="Asia/Shanghai", working_days="1,2,3,4,5", work_start_hour=9,
        work_end_hour=17, currency="CNY",
    )
    return Supplier(**(base | over))


async def _sent(session, supplier, *, number, sent, acknowledged=None, **over):
    order = PurchaseOrder(
        number=number, supplier_id=supplier.id, status="acknowledged" if acknowledged else "sent",
        total_value=1000, sent_at=sent, acknowledged_at=acknowledged, **over,
    )
    session.add(order)
    await session.flush()
    return order


async def _reply(session, order, kind="acknowledgement", *, confidence=0.9):
    session.add(
        InboundMessage(
            external_id=f"<{order.number}-{kind}-{confidence}@x>",
            purchase_order_id=order.id, supplier_id=order.supplier_id,
            body="", received_at=FRIDAY, kind=kind, confidence=confidence,
        )
    )
    await session.flush()


@pytest.mark.asyncio
async def test_an_empty_database_reports_zero_rather_than_failing(session):
    """The first thing anyone sees is a system with nothing in it."""
    out = await CoordinationService(session).collect()
    assert out.round_trips.orders == 0
    assert out.round_trips.per_order is None
    assert out.cycle.median_hours is None
    assert out.zones == []


@pytest.mark.asyncio
async def test_replies_per_order_is_the_round_trip_count(session):
    supplier = _supplier()
    session.add(supplier)
    await session.flush()
    a = await _sent(session, supplier, number="PO-1", sent=FRIDAY)
    b = await _sent(session, supplier, number="PO-2", sent=FRIDAY)
    await _reply(session, a)
    await _reply(session, a, kind="delay")
    await _reply(session, b)

    out = await CoordinationService(session).collect()
    assert out.round_trips.orders == 2
    assert out.round_trips.replies == 3
    assert out.round_trips.per_order == 1.5


@pytest.mark.asyncio
async def test_a_question_counts_as_an_avoidable_round_trip(session):
    """The whole completeness argument rests on this number, so it is counted
    apart from every other kind of reply rather than folded into a total."""
    supplier = _supplier()
    session.add(supplier)
    await session.flush()
    order = await _sent(session, supplier, number="PO-1", sent=FRIDAY)
    await _reply(session, order)
    await _reply(session, order, kind="question")

    out = await CoordinationService(session).collect()
    assert out.round_trips.clarifications == 1
    assert out.round_trips.avoidable_share == 0.5


@pytest.mark.asyncio
async def test_an_unmatched_reply_does_not_inflate_anyones_round_trips(session):
    """A message nobody could attach to an order is a resolution failure, not a
    round trip belonging to whichever order happens to be open."""
    supplier = _supplier()
    session.add(supplier)
    await session.flush()
    await _sent(session, supplier, number="PO-1", sent=FRIDAY)
    session.add(
        InboundMessage(
            external_id="<orphan@x>", body="", received_at=FRIDAY,
            kind="unknown", confidence=0.0,
        )
    )
    await session.flush()

    out = await CoordinationService(session).collect()
    assert out.round_trips.replies == 0


@pytest.mark.asyncio
async def test_working_hours_are_reported_apart_from_wall_clock_hours(session):
    """An order sent as Shanghai closes on Friday and acknowledged on Monday
    morning waited three days, of which the supplier was present for one hour.
    Reporting only the three days measures the timezone, not the supplier."""
    supplier = _supplier()
    session.add(supplier)
    await session.flush()
    sent = datetime(2026, 8, 14, 8, 30, tzinfo=UTC)  # 16:30 Friday in Shanghai
    acknowledged = datetime(2026, 8, 17, 1, 30, tzinfo=UTC)  # 09:30 Monday
    await _sent(session, supplier, number="PO-1", sent=sent, acknowledged=acknowledged)

    out = await CoordinationService(session).collect()
    assert out.cycle.acknowledged == 1
    assert out.cycle.median_hours == 65.0
    assert out.cycle.median_working_hours == 1.0


@pytest.mark.asyncio
async def test_working_hours_can_never_exceed_elapsed_hours(session):
    """A reply that arrived in one minute used to count as half an hour of
    working time, which reported more work than time — impossible, and visible
    on any dashboard showing both."""
    supplier = _supplier(work_start_hour=0, work_end_hour=24, working_days="1,2,3,4,5,6,7")
    hours = WorkingHours.from_supplier(supplier)
    start = datetime(2026, 8, 14, 9, 0, tzinfo=UTC)
    assert working_hours_between(hours, start, start + timedelta(minutes=1)) < 0.02


@pytest.mark.asyncio
async def test_orders_awaiting_a_reply_are_counted_but_not_timed(session):
    supplier = _supplier()
    session.add(supplier)
    await session.flush()
    await _sent(session, supplier, number="PO-1", sent=FRIDAY)

    out = await CoordinationService(session).collect()
    assert out.cycle.awaiting == 1
    assert out.cycle.acknowledged == 0
    assert out.cycle.median_hours is None


@pytest.mark.asyncio
async def test_zones_are_ordered_with_the_least_overlap_first(session):
    """Where a removed round trip is worth most is where anyone should look
    first, so it is not left to the reader to sort the table.

    Collected against FRIDAY rather than the wall clock. Overlap is measured on
    the day it is asked about, and both of these suppliers keep Monday to Friday
    hours — so run on a Saturday this compared nothing to nothing and failed on
    an ordering the code had got right.
    """
    near = _supplier(code="TR", name="Istanbul Packaging", timezone="Europe/Istanbul")
    far = _supplier(code="US", name="Chicago Components", timezone="America/Chicago")
    session.add_all([near, far])
    await session.flush()
    await _sent(session, near, number="PO-1", sent=FRIDAY)
    await _sent(session, far, number="PO-2", sent=FRIDAY)

    out = await CoordinationService(session).collect(now=FRIDAY)
    assert [z.timezone for z in out.zones] == ["America/Chicago", "Europe/Istanbul"]
    assert out.zones[0].overlap_hours < out.zones[1].overlap_hours


@pytest.mark.asyncio
async def test_a_message_filed_for_a_human_is_not_counted_as_autonomous(session):
    supplier = _supplier()
    session.add(supplier)
    await session.flush()
    order = await _sent(session, supplier, number="PO-1", sent=FRIDAY)
    await _reply(session, order)
    await _reply(session, order, kind="unknown", confidence=0.1)
    session.add(
        Issue(
            purchase_order_id=order.id, kind="unparsed_message", severity="low",
            detail="could not be applied", suggested_action="read it",
        )
    )
    await session.flush()

    out = await CoordinationService(session).collect()
    assert out.autonomy.read == 2
    assert out.autonomy.filed_for_a_human == 1
    assert out.autonomy.acted == 1
    assert out.autonomy.acted_share == 0.5


@pytest.mark.asyncio
async def test_an_amended_order_is_not_counted_as_approved_unchanged(session):
    """The automation-bias indicator. A gate whose prepared orders are always
    approved unamended is either preparing well or being waved through, and the
    two look identical from inside — so the number has to be the real one."""
    supplier = _supplier()
    session.add(supplier)
    await session.flush()
    await _sent(
        session, supplier, number="PO-1", sent=FRIDAY,
        requires_approval=True, approved_at=FRIDAY, approved_by="buyer",
    )
    await _sent(
        session, supplier, number="PO-2", sent=FRIDAY,
        requires_approval=True, approved_at=FRIDAY, approved_by="buyer", revision=1,
    )

    out = await CoordinationService(session).collect()
    assert out.approval.needed == 2
    assert out.approval.granted == 2
    assert out.approval.revised == 1
    assert out.approval.without_amendment == 1


@pytest.mark.asyncio
async def test_the_endpoint_needs_the_api_key(client):
    result = await client.get("/coordination", headers={"X-API-Key": "wrong"})
    assert result.status_code == 401


@pytest.mark.asyncio
async def test_the_endpoint_answers_on_an_empty_database(client):
    result = await client.get("/coordination")
    assert result.status_code == 200
    assert result.json()["round_trips"]["orders"] == 0


@pytest.mark.asyncio
async def test_the_book_splits_open_orders_by_who_has_to_move_next(session):
    """"Approved" and "sent" are one step apart in the model and a world apart
    on a Tuesday morning: one is work nobody has done, the other is a wait."""
    supplier = _supplier()
    session.add(supplier)
    await session.flush()
    for number, state in (
        ("PO-1", "draft"), ("PO-2", "pending_approval"), ("PO-3", "approved"),
        ("PO-4", "sent"), ("PO-5", "acknowledged"), ("PO-6", "in_transit"),
    ):
        session.add(PurchaseOrder(
            number=number, supplier_id=supplier.id, status=state, total_value=100,
        ))
    await session.flush()

    book = (await CoordinationService(session).collect()).book
    assert book.with_us == 3
    assert book.to_approve == 1
    assert book.to_send == 1
    assert book.with_supplier == 3
    assert book.unacknowledged == 1
    assert book.total == 6


@pytest.mark.asyncio
async def test_received_value_is_kept_per_currency_rather_than_summed(session):
    """Two mills invoicing in different money do not add up, and one total
    would be a figure that is true of neither."""
    cny = _supplier()
    sar = _supplier(code="RUH-BOX", name="Riyadh Cartons", email="s@rc.sa",
                    timezone="Asia/Riyadh", currency="SAR")
    session.add_all([cny, sar])
    await session.flush()
    session.add_all([
        PurchaseOrder(number="PO-1", supplier_id=cny.id, status="received",
                      currency="CNY", total_value=1000),
        PurchaseOrder(number="PO-2", supplier_id=cny.id, status="received",
                      currency="CNY", total_value=500),
        PurchaseOrder(number="PO-3", supplier_id=sar.id, status="received",
                      currency="SAR", total_value=200),
        PurchaseOrder(number="PO-4", supplier_id=sar.id, status="cancelled",
                      currency="SAR", total_value=900),
    ])
    await session.flush()

    book = (await CoordinationService(session).collect()).book
    assert book.received == 3
    assert book.cancelled == 1
    assert book.received_value == {"CNY": 1500.0, "SAR": 200.0}


@pytest.mark.asyncio
async def test_an_empty_book_reports_zero_rather_than_nothing(session):
    book = (await CoordinationService(session).collect()).book
    assert book.total == 0
    assert book.with_us == 0
    assert book.received_value == {}
