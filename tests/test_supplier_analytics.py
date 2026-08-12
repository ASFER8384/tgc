"""What a supplier's record says, and — more importantly — what it refuses to say.

The failure mode worth testing for here is not a wrong number. It is a confident
zero: an empty history rendering as "never late", "no trouble", "nothing owed",
when the truth is that nothing has been measured yet. Most of these tests are
about the difference between a measured zero and an absent one.
"""

from datetime import UTC, datetime, timedelta

import pytest

from sca.analytics.supplier import SupplierAnalyticsService
from sca.models import (
    AuditLog,
    InboundMessage,
    Issue,
    Item,
    PurchaseOrder,
    PurchaseOrderLine,
    Supplier,
    SupplierItem,
)

SENT = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)


def _supplier(code="GZ-TEX", name="Guangzhou Silk Mill", **over) -> Supplier:
    base = dict(
        code=code, name=name, country="CN", email=f"orders@{code.lower()}.cn",
        timezone="Asia/Shanghai", working_days="1,2,3,4,5",
        work_start_hour=9, work_end_hour=17, lead_time_days=21, currency="SAR",
    )
    return Supplier(**(base | over))


async def _item(session, sku, supplier):
    session.add(Item(sku=sku, name=sku.title(), supplier_id=supplier.id, unit_cost=10))
    session.add(SupplierItem(
        supplier_id=supplier.id, sku=sku, unit_cost=10, currency="SAR",
        moq=1, pack_size=1, active=True,
    ))
    await session.flush()


async def _order(session, supplier, number, **over):
    order = PurchaseOrder(**(dict(
        number=number, supplier_id=supplier.id, status="draft", total_value=1000,
        currency="SAR",
    ) | over))
    session.add(order)
    await session.flush()
    return order


# ------------------------------------------------------------------ exclusivity
@pytest.mark.asyncio
async def test_an_item_only_they_make_is_counted_as_exposure(session):
    """The one measure that works on a supplier added five minutes ago, and the
    reason it leads: it needs no history at all."""
    mill = _supplier()
    session.add(mill)
    await session.flush()
    await _item(session, "ALN-ABAYA-01", mill)

    out = await SupplierAnalyticsService(session).collect(mill)
    assert out.exclusivity.items == 1
    assert out.exclusivity.only_source == 1
    assert out.exclusivity.only_source_skus == ["ALN-ABAYA-01"]


@pytest.mark.asyncio
async def test_a_second_supplier_removes_the_exposure(session):
    mill, other = _supplier(), _supplier(code="IST-TX", name="Istanbul Textile")
    session.add_all([mill, other])
    await session.flush()
    await _item(session, "ALN-SILK-NVY", mill)
    session.add(SupplierItem(
        supplier_id=other.id, sku="ALN-SILK-NVY", unit_cost=9, currency="SAR",
        moq=1, pack_size=1, active=True,
    ))
    await session.flush()

    out = await SupplierAnalyticsService(session).collect(mill)
    assert out.exclusivity.items == 1
    assert out.exclusivity.only_source == 0


@pytest.mark.asyncio
async def test_an_inactive_link_does_not_count_as_a_second_source(session):
    """A supplier who has been unticked cannot cover a stockout, so counting them
    would report the exposure as closed while it is open."""
    mill, other = _supplier(), _supplier(code="IST-TX", name="Istanbul Textile")
    session.add_all([mill, other])
    await session.flush()
    await _item(session, "ALN-SILK-NVY", mill)
    session.add(SupplierItem(
        supplier_id=other.id, sku="ALN-SILK-NVY", unit_cost=9, currency="SAR",
        moq=1, pack_size=1, active=False,
    ))
    await session.flush()

    out = await SupplierAnalyticsService(session).collect(mill)
    assert out.exclusivity.only_source == 1


# ------------------------------------------------------------------- commitment
@pytest.mark.asyncio
async def test_cancelled_orders_are_counted_but_not_spent(session):
    """Money that was never going to leave is not commitment. The abandoned order
    is still a fact about the supplier or the plan, so it keeps its own line."""
    mill = _supplier()
    session.add(mill)
    await session.flush()
    await _order(session, mill, "PO-1", status="received", total_value=1000)
    await _order(session, mill, "PO-2", status="sent", total_value=500)
    await _order(session, mill, "PO-3", status="cancelled", total_value=9999)

    out = await SupplierAnalyticsService(session).collect(mill)
    assert out.commitment.orders == 3
    assert out.commitment.cancelled == 1
    assert out.commitment.received == 1
    assert out.commitment.live == 1
    assert out.commitment.value == "1500.00"


@pytest.mark.asyncio
async def test_a_supplier_never_ordered_from_reports_nothing_rather_than_zero(session):
    """Set up, priced, never bought from. The console says so in words — a bare
    0.00 reads as a supplier who is cheap, not one who is untested."""
    mill = _supplier()
    session.add(mill)
    await session.flush()

    out = await SupplierAnalyticsService(session).collect(mill)
    assert out.commitment.orders == 0
    assert out.exchanges.sent == 0
    assert out.exchanges.per_order is None


# -------------------------------------------------------------------- exchanges
@pytest.mark.asyncio
async def test_replies_that_ask_us_something_are_counted_apart(session):
    """The headroom claim: a question back is a full wait the outbound message
    could have prevented. Folding it into a reply total would hide it."""
    mill = _supplier()
    session.add(mill)
    await session.flush()
    order = await _order(
        session, mill, "PO-1", status="sent", sent_at=SENT,
        acknowledged_at=SENT + timedelta(hours=6),
    )
    for index, kind in enumerate(("acknowledgement", "question", "acknowledgement")):
        session.add(InboundMessage(
            external_id=f"m-{index}", source="email", from_address="orders@gz.cn",
            subject="Re: PO-1", body="…", received_at=SENT + timedelta(hours=6),
            kind=kind, confidence=0.9, supplier_id=mill.id,
            purchase_order_id=order.id,
        ))
    await session.flush()

    out = await SupplierAnalyticsService(session).collect(mill)
    assert out.exchanges.replies == 3
    assert out.exchanges.clarifications == 1
    assert out.exchanges.per_order == 3.0
    assert out.exchanges.avoidable_share == pytest.approx(0.333, abs=0.001)
    assert out.exchanges.median_hours == 6.0


# ------------------------------------------------------------------------- kept
@pytest.mark.asyncio
async def test_a_late_delivery_is_measured_against_the_date_they_confirmed(session):
    mill = _supplier()
    session.add(mill)
    await session.flush()
    promised = SENT + timedelta(days=20)
    order = await _order(
        session, mill, "PO-1", status="received", sent_at=SENT,
        confirmed_delivery_date=promised,
    )
    session.add(AuditLog(
        actor="buyer", action="order.receive", entity="purchase_order",
        entity_id=order.id, meta={}, created_at=promised + timedelta(days=3),
    ))
    await session.flush()

    out = await SupplierAnalyticsService(session).collect(mill)
    assert out.kept.checked == 1
    assert out.kept.on_time == 0
    assert out.kept.median_days_late == 3.0
    assert "1 completed order" in out.kept.basis


@pytest.mark.asyncio
async def test_arriving_on_the_promised_day_counts_as_on_time(session):
    """A receipt booked in a few hours past midnight on the promised date is a
    clock, not a late delivery."""
    mill = _supplier()
    session.add(mill)
    await session.flush()
    promised = SENT + timedelta(days=20)
    order = await _order(
        session, mill, "PO-1", status="received", sent_at=SENT,
        confirmed_delivery_date=promised,
    )
    session.add(AuditLog(
        actor="buyer", action="order.receive", entity="purchase_order",
        entity_id=order.id, meta={}, created_at=promised + timedelta(hours=9),
    ))
    await session.flush()

    out = await SupplierAnalyticsService(session).collect(mill)
    assert out.kept.checked == 1
    assert out.kept.on_time == 1


@pytest.mark.asyncio
async def test_same_day_receipts_are_refused_and_the_refusal_is_explained(session):
    """Sent and booked in within the day is a demo click or a backfill. Averaging
    those reports a supplier who delivers instantly, which is worse than
    reporting nothing because it looks measured."""
    mill = _supplier()
    session.add(mill)
    await session.flush()
    order = await _order(
        session, mill, "PO-1", status="received", sent_at=SENT,
        confirmed_delivery_date=SENT + timedelta(days=20),
    )
    session.add(AuditLog(
        actor="buyer", action="order.receive", entity="purchase_order",
        entity_id=order.id, meta={}, created_at=SENT + timedelta(minutes=4),
    ))
    await session.flush()

    out = await SupplierAnalyticsService(session).collect(mill)
    assert out.kept.checked == 0
    assert out.kept.median_days_late is None
    assert "same day it was sent" in out.kept.basis


@pytest.mark.asyncio
async def test_no_history_says_so_rather_than_reporting_perfect(session):
    mill = _supplier()
    session.add(mill)
    await session.flush()

    out = await SupplierAnalyticsService(session).collect(mill)
    assert out.kept.checked == 0
    assert out.kept.on_time == 0
    assert "no completed order" in out.kept.basis


# ------------------------------------------------------------------------ short
@pytest.mark.asyncio
async def test_a_short_shipment_is_counted_in_lines_and_units(session):
    mill = _supplier()
    session.add(mill)
    await session.flush()
    order = await _order(session, mill, "PO-1", status="received")
    session.add_all([
        PurchaseOrderLine(
            purchase_order_id=order.id, sku="A", description="A", quantity=100,
            unit_price=10, line_total=1000, received_quantity=80,
        ),
        PurchaseOrderLine(
            purchase_order_id=order.id, sku="B", description="B", quantity=50,
            unit_price=10, line_total=500, received_quantity=50,
        ),
    ])
    await session.flush()

    out = await SupplierAnalyticsService(session).collect(mill)
    assert out.short.lines_received == 2
    assert out.short.lines_short == 1
    assert out.short.units_short == 20


# ---------------------------------------------------------------------- trouble
@pytest.mark.asyncio
async def test_issues_are_reported_by_kind_and_never_as_a_rate(session):
    """Two issues against forty orders and two against two are different
    situations. A rate would render them identically."""
    mill = _supplier()
    session.add(mill)
    await session.flush()
    session.add_all([
        Issue(supplier_id=mill.id, kind="eta_slip", severity="medium",
              detail="moved the date", suggested_action="chase", status="resolved"),
        Issue(supplier_id=mill.id, kind="short_delivery", severity="high",
              detail="80 of 100", suggested_action="claim", status="open"),
    ])
    await session.flush()

    out = await SupplierAnalyticsService(session).collect(mill)
    assert out.trouble.total == 2
    assert out.trouble.open_now == 1
    assert out.trouble.by_kind == {"eta_slip": 1, "short_delivery": 1}


@pytest.mark.asyncio
async def test_another_suppliers_record_does_not_leak_into_this_one(session):
    mill, other = _supplier(), _supplier(code="IST-TX", name="Istanbul Textile")
    session.add_all([mill, other])
    await session.flush()
    await _order(session, other, "PO-9", status="received", total_value=5000)
    session.add(Issue(
        supplier_id=other.id, kind="eta_slip", severity="medium",
        detail="theirs", suggested_action="chase", status="open",
    ))
    await session.flush()

    out = await SupplierAnalyticsService(session).collect(mill)
    assert out.commitment.orders == 0
    assert out.trouble.total == 0
