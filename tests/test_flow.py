"""The whole path through the API: stock in, order out, reply back, exception up."""

from datetime import UTC, datetime, timedelta

import pytest

from sca.models import PurchaseOrder, Supplier
from sca.orders.service import OrderError, OrderService
from sca.planning.service import PlanningService


async def _setup(client, supplier_payload, **item_overrides):
    # Always open, so these tests do not pass or fail depending on what time the
    # suite happens to run. Working hours are exercised deliberately, with a fixed
    # clock, in test_send_outside_working_hours_queues_instead_of_sending.
    always_open = supplier_payload | {
        "working_days": "1,2,3,4,5,6,7", "work_start_hour": 0, "work_end_hour": 24,
    }
    supplier = (await client.post("/suppliers", json=always_open)).json()
    item = {
        "sku": "ALN-SILK-NVY", "name": "Navy silk", "supplier_id": supplier["id"],
        "category": "fabric", "brand": "aleena", "unit": "m", "moq": 300,
        "pack_size": 50, "unit_cost": "42.00",
    } | item_overrides
    await client.post("/items", json=item)
    return supplier


async def test_low_cover_produces_a_suggestion_respecting_moq_and_pack(client, supplier_payload):
    supplier = await _setup(client, supplier_payload)
    # 260 on hand against 90 a week is under three weeks, and the mill takes six.
    await client.post("/stock", json={
        "sku": "ALN-SILK-NVY", "on_hand": 260, "on_order": 0, "weekly_forecast": "90",
    })
    body = (await client.post("/planning/suggest")).json()
    assert body["count"] == 1
    line = body["by_supplier"][0]["lines"][0]
    # Target is eight weeks, 720 units, less 260 on hand is 460, rounded up to a
    # multiple of the 50 unit pack.
    assert line["suggest_quantity"] == 500
    assert line["suggest_quantity"] % 50 == 0
    assert supplier["id"] == body["by_supplier"][0]["supplier_id"]


async def test_healthy_cover_suggests_nothing(client, supplier_payload):
    await _setup(client, supplier_payload)
    await client.post("/stock", json={
        "sku": "ALN-SILK-NVY", "on_hand": 1400, "on_order": 300, "weekly_forecast": "85",
    })
    assert (await client.post("/planning/suggest")).json()["count"] == 0


async def test_no_forecast_means_no_opinion(client, supplier_payload):
    await _setup(client, supplier_payload)
    await client.post("/stock", json={
        "sku": "ALN-SILK-NVY", "on_hand": 0, "on_order": 0, "weekly_forecast": "0",
    })
    assert (await client.post("/planning/suggest")).json()["count"] == 0


async def test_new_supplier_order_needs_approval_before_it_can_be_sent(
    client, supplier_payload
):
    await _setup(client, supplier_payload)
    await client.post("/stock", json={
        "sku": "ALN-SILK-NVY", "on_hand": 260, "on_order": 0, "weekly_forecast": "90",
    })
    created = (await client.post("/planning/create-orders")).json()
    number = created["orders"][0]["number"]
    assert created["orders"][0]["status"] == "pending_approval"

    blocked = await client.post(f"/purchase-orders/{number}/send")
    assert blocked.status_code == 409

    approved = (await client.post(f"/purchase-orders/{number}/approve",
                                  json={"approver": "buyer"})).json()
    assert approved["status"] == "approved"
    assert approved["approved_by"] == "buyer"


async def test_send_outside_working_hours_queues_instead_of_sending(
    session, supplier_payload
):
    supplier = Supplier(**supplier_payload)
    session.add(supplier)
    await session.flush()
    service = OrderService(session)
    order = await service.create(
        supplier.id, [{"sku": "X", "quantity": 10, "unit_price": "5.00"}]
    )
    await service.approve(order, approver="buyer")

    # 22:00 in Guangzhou, four hours after the mill closes.
    night = datetime(2026, 8, 10, 14, 0, tzinfo=UTC)
    result = await service.send(order, now=night)
    assert result["sent"] is False
    assert order.status == "approved"
    assert result["scheduled_send_at"].astimezone(
        __import__("zoneinfo").ZoneInfo("Asia/Shanghai")
    ).hour == 8

    morning = result["scheduled_send_at"]
    assert (await service.send(order, now=morning))["sent"] is True
    assert order.status == "sent"


async def test_illegal_transition_is_refused(session, supplier_payload):
    supplier = Supplier(**supplier_payload)
    session.add(supplier)
    await session.flush()
    service = OrderService(session)
    order = await service.create(supplier.id, [{"sku": "X", "quantity": 1, "unit_price": "1"}])
    order.status = "received"
    with pytest.raises(OrderError):
        await service.approve(order, approver="buyer")


async def test_confirmation_email_acknowledges_the_order(client, supplier_payload):
    supplier = await _setup(client, supplier_payload)
    await client.post("/stock", json={
        "sku": "ALN-SILK-NVY", "on_hand": 260, "on_order": 0, "weekly_forecast": "90",
    })
    created = (await client.post("/planning/create-orders")).json()
    number = created["orders"][0]["number"]
    await client.post(f"/purchase-orders/{number}/approve", json={"approver": "buyer"})
    await client.post(f"/purchase-orders/{number}/send")

    result = (await client.post("/inbound/email", json={
        "external_id": "msg-1",
        "from_address": supplier["email"],
        "subject": f"Re: {number} confirmation",
        "body": f"We confirm {number} and will ship on 2026-09-28.",
    })).json()
    assert result["kind"] == "acknowledgement"
    assert result["purchase_order"] == number

    detail = (await client.get(f"/purchase-orders/{number}")).json()
    assert detail["status"] in ("acknowledged", "sent")
    assert detail["acknowledged_at"] is not None


async def test_a_far_off_promised_date_raises_an_exception(client, supplier_payload):
    supplier = await _setup(client, supplier_payload)
    await client.post("/stock", json={
        "sku": "ALN-SILK-NVY", "on_hand": 260, "on_order": 0, "weekly_forecast": "90",
    })
    created = (await client.post("/planning/create-orders")).json()
    number = created["orders"][0]["number"]
    await client.post(f"/purchase-orders/{number}/approve", json={"approver": "buyer"})
    await client.post(f"/purchase-orders/{number}/send")

    far = (datetime.now(UTC) + timedelta(days=200)).date().isoformat()
    await client.post("/inbound/email", json={
        "external_id": "msg-2",
        "from_address": supplier["email"],
        "subject": f"RE: {number}",
        "body": f"We confirm {number}, shipment will be delayed to {far}.",
    })
    issues = (await client.get("/issues")).json()
    assert any(i["kind"] == "eta_slip" for i in issues)


async def test_unreadable_reply_is_filed_for_a_human_not_guessed(client, supplier_payload):
    supplier = await _setup(client, supplier_payload)
    result = (await client.post("/inbound/email", json={
        "external_id": "msg-3",
        "from_address": supplier["email"],
        "subject": "شكرا",
        "body": "Thanks, we will revert.",
    })).json()
    assert result["actions"] == ["filed for a human"]
    issues = (await client.get("/issues")).json()
    assert any(i["kind"] == "unparsed_message" for i in issues)


async def test_the_same_email_twice_is_ignored(client, supplier_payload):
    supplier = await _setup(client, supplier_payload)
    payload = {
        "external_id": "msg-4", "from_address": supplier["email"],
        "subject": "Re: PO-9999", "body": "We confirm.",
    }
    first = (await client.post("/inbound/email", json=payload)).json()
    second = (await client.post("/inbound/email", json=payload)).json()
    assert first["duplicate"] is False
    assert second["duplicate"] is True


async def test_short_shipment_raises_an_exception_at_receiving(client, supplier_payload):
    supplier = await _setup(client, supplier_payload)
    order = (await client.post("/purchase-orders", json={
        "supplier_id": supplier["id"],
        "lines": [{"sku": "ALN-SILK-NVY", "quantity": 500, "unit_price": "42.00"}],
    })).json()
    number = order["number"]
    await client.post(f"/purchase-orders/{number}/approve", json={"approver": "buyer"})
    await client.post(f"/purchase-orders/{number}/send")
    await client.post("/inbound/email", json={
        "external_id": "msg-5", "from_address": supplier["email"],
        "subject": f"Re: {number}", "body": f"We confirm {number}, ship 2026-09-28.",
    })
    received = (await client.post(f"/purchase-orders/{number}/receive",
                                  json={"received": {"ALN-SILK-NVY": 450}})).json()
    assert received["status"] == "received"
    assert any("450 received of 500" in detail for detail in received["issues_raised"])


async def test_silent_supplier_is_chased_once(session, supplier_payload):
    supplier = Supplier(**supplier_payload)
    session.add(supplier)
    await session.flush()
    service = OrderService(session)
    order = await service.create(supplier.id, [{"sku": "X", "quantity": 1, "unit_price": "1"}])
    await service.approve(order, approver="buyer")
    # Monday 10:00 Guangzhou.
    sent_at = datetime(2026, 8, 10, 2, 0, tzinfo=UTC)
    await service.send(order, now=sent_at)

    raised = await service.sweep_unacknowledged(now=sent_at + timedelta(hours=30))
    assert len(raised) == 1
    assert raised[0].kind == "no_acknowledgement"
    # Once, not on every sweep, or the buyer learns to ignore them.
    assert await service.sweep_unacknowledged(now=sent_at + timedelta(hours=54)) == []


async def test_shipment_tracking_moves_the_order_in_transit(client, supplier_payload):
    supplier = await _setup(client, supplier_payload)
    order = (await client.post("/purchase-orders", json={
        "supplier_id": supplier["id"],
        "lines": [{"sku": "ALN-SILK-NVY", "quantity": 100, "unit_price": "42.00"}],
    })).json()
    number = order["number"]
    await client.post(f"/purchase-orders/{number}/approve", json={"approver": "buyer"})
    await client.post(f"/purchase-orders/{number}/send")
    await client.post("/inbound/email", json={
        "external_id": "msg-6", "from_address": supplier["email"],
        "subject": f"Re: {number}", "body": f"Confirmed {number}, ship 2026-09-28.",
    })
    tracked = (await client.post(f"/purchase-orders/{number}/shipment",
                                 json={"carrier": "mock", "tracking_number": "MOCK12345"})).json()
    assert tracked["status"]
    assert tracked["order_status"] in ("acknowledged", "in_transit")


async def test_api_requires_a_key(client):
    client.headers.pop("X-API-Key")
    assert (await client.get("/suppliers")).status_code == 401


async def test_planning_service_ignores_inactive_suppliers(session, supplier_payload):
    from sca.models import Item, StockSnapshot

    supplier = Supplier(**supplier_payload)
    supplier.active = False
    session.add(supplier)
    await session.flush()
    session.add(Item(sku="X", name="X", supplier_id=supplier.id, moq=1, pack_size=1, unit_cost=1))
    session.add(StockSnapshot(sku="X", on_hand=0, on_order=0, weekly_forecast=10))
    await session.flush()
    assert await PlanningService(session).suggest() == []


async def test_order_numbers_are_sequential(session, supplier_payload):
    supplier = Supplier(**supplier_payload)
    session.add(supplier)
    await session.flush()
    service = OrderService(session)
    first = await service.create(supplier.id, [{"sku": "X", "quantity": 1, "unit_price": "1"}])
    second = await service.create(supplier.id, [{"sku": "Y", "quantity": 1, "unit_price": "1"}])
    assert first.number != second.number
    assert (await session.get(PurchaseOrder, second.id)).number.startswith("PO-")
