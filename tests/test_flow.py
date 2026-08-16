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

    # Thirty hours later is only sixteen working hours for a mill that closes at
    # 18:00: their own night is not a delay, and chasing here would be chasing a
    # supplier who is not late.
    assert await service.sweep_unacknowledged(now=sent_at + timedelta(hours=30)) == []

    # Fifty two hours is exactly twenty four of their working hours.
    raised = await service.sweep_unacknowledged(now=sent_at + timedelta(hours=52))
    assert len(raised) == 1
    assert raised[0].kind == "no_acknowledgement"
    assert raised[0].context["working_hours_waited"] == 24.0
    # Once, not on every sweep, or the buyer learns to ignore them.
    assert await service.sweep_unacknowledged(now=sent_at + timedelta(hours=76)) == []


async def _sent_order(client, supplier_payload):
    supplier = await _setup(client, supplier_payload)
    await client.post("/stock", json={
        "sku": "ALN-SILK-NVY", "on_hand": 260, "on_order": 0, "weekly_forecast": "90",
    })
    created = (await client.post("/planning/create-orders")).json()
    order = created["orders"][0]
    number = order["number"]
    await client.post(f"/purchase-orders/{number}/approve", json={"approver": "buyer"})
    await client.post(f"/purchase-orders/{number}/send")
    return supplier, number, float(order["total_value"])


async def test_confirming_at_a_different_value_is_raised_before_the_invoice(
    client, supplier_payload
):
    """A supplier repricing at acknowledgement is the expensive quiet case: the
    order has not shipped, so the difference is still negotiable."""
    supplier, number, ordered = await _sent_order(client, supplier_payload)
    result = (await client.post("/inbound/email", json={
        "external_id": "msg-reprice",
        "from_address": supplier["email"],
        # Currency after the number, the way suppliers usually write it.
        "subject": f"Re: {number}",
        "body": f"We confirm {number} and will ship on 2026-09-28. {ordered + 300:,.2f} CNY",
    })).json()
    assert result["kind"] == "acknowledgement"
    assert "confirmed value differs from the order" in result["actions"]

    issues = (await client.get("/issues")).json()
    priced = [i for i in issues if i["kind"] == "price_mismatch"]
    assert len(priced) == 1
    assert priced[0]["severity"] == "medium"


async def test_a_confirmation_quoting_the_right_total_and_a_deposit_is_silent(
    client, supplier_payload
):
    """Several amounts in one reply must not trip the check when one of them is
    the agreed total. Matching only the largest would flag every deposit."""
    supplier, number, ordered = await _sent_order(client, supplier_payload)
    result = (await client.post("/inbound/email", json={
        "external_id": "msg-deposit",
        "from_address": supplier["email"],
        "subject": f"Re: {number}",
        "body": (
            f"We confirm {number}, total {ordered:,.2f} CNY, "
            f"30% deposit of {ordered * 0.3:,.2f} CNY due before production."
        ),
    })).json()
    assert "confirmed value differs from the order" not in result["actions"]
    issues = (await client.get("/issues")).json()
    assert [i for i in issues if i["kind"] == "price_mismatch"] == []


async def test_ordering_a_line_stops_it_being_suggested_again(client, supplier_payload):
    """The duplicate order bug: cover is on hand plus on order, so an order that
    leaves on order at zero suggests the same purchase on every refresh."""
    await _setup(client, supplier_payload)
    await client.post("/stock", json={
        "sku": "ALN-SILK-NVY", "on_hand": 260, "on_order": 0, "weekly_forecast": "90",
    })
    assert (await client.post("/planning/suggest")).json()["count"] == 1

    created = (await client.post("/planning/create-orders")).json()
    number = created["orders"][0]["number"]
    # The need is covered by a draft that exists, so it is no longer a need.
    assert (await client.post("/planning/suggest")).json()["count"] == 0

    # And cancelling puts it back, which is the point of cancelling.
    await client.post(f"/purchase-orders/{number}/cancel", json={"reason": "too expensive"})
    assert (await client.post("/planning/suggest")).json()["count"] == 1


async def test_receiving_moves_stock_from_on_order_to_on_hand(client, supplier_payload):
    await _setup(client, supplier_payload)
    await client.post("/stock", json={
        "sku": "ALN-SILK-NVY", "on_hand": 260, "on_order": 0, "weekly_forecast": "90",
    })
    created = (await client.post("/planning/create-orders")).json()
    number = created["orders"][0]["number"]
    await client.post(f"/purchase-orders/{number}/approve", json={"approver": "buyer"})
    await client.post(f"/purchase-orders/{number}/send")
    await client.post("/inbound/email", json={
        "external_id": "msg-stock", "from_address": supplier_payload["email"],
        "subject": f"Re: {number}", "body": f"We confirm {number}.",
    })
    await client.post(f"/purchase-orders/{number}/receive", json={"received": {}})

    items = (await client.get("/items")).json()
    row = next(i for i in items if i["sku"] == "ALN-SILK-NVY")
    # 260 on hand plus the 500 ordered, and nothing still outstanding.
    assert row["on_hand"] == 760
    assert row["on_order"] == 0


async def test_revising_a_repriced_order_reopens_it_and_closes_the_issue(
    client, supplier_payload
):
    """The negotiation loop: they reprice, we counter, the order is live again."""
    supplier, number, ordered = await _sent_order(client, supplier_payload)
    await client.post("/inbound/email", json={
        "external_id": "msg-reprice-2",
        "from_address": supplier["email"],
        "subject": f"Re: {number}",
        "body": f"We confirm {number} and will ship on 2026-09-28. {ordered + 5000:,.2f} CNY",
    })
    assert any(i["kind"] == "price_mismatch" for i in (await client.get("/issues")).json())

    detail = (await client.get(f"/purchase-orders/{number}")).json()
    line = detail["lines"][0]
    revised = (await client.post(f"/purchase-orders/{number}/revise", json={
        "lines": [{
            "sku": line["sku"], "quantity": line["quantity"],
            "unit_price": str(round(float(line["unit_price"]) * 0.95, 2)),
        }],
        "reason": "countered their increase, holding last agreed price less 5%",
    })).json()

    assert revised["revision"] == 1
    assert float(revised["total_value"]) < ordered
    # Sendable again, and everything the supplier said about the old price is gone.
    assert revised["status"] in ("approved", "pending_approval")
    assert revised["acknowledged_at"] is None
    assert revised["confirmed_delivery_date"] is None
    # The exception is answered, not left to be re-read every morning.
    assert [i for i in (await client.get("/issues")).json()
            if i["kind"] == "price_mismatch"] == []


async def test_a_revision_that_crosses_the_threshold_needs_approval_again(
    client, supplier_payload
):
    """The person who approved the original never saw the new number."""
    supplier, number, _ = await _sent_order(client, supplier_payload)
    detail = (await client.get(f"/purchase-orders/{number}")).json()
    line = detail["lines"][0]
    revised = (await client.post(f"/purchase-orders/{number}/revise", json={
        "lines": [{"sku": line["sku"], "quantity": line["quantity"], "unit_price": "900.00"}],
        "reason": "accepted their raw material surcharge",
    })).json()
    assert revised["status"] == "pending_approval"
    assert "approval threshold" in revised["approval_reason"]


async def test_an_unacceptable_price_can_be_walked_away_from(client, supplier_payload):
    supplier, number, _ = await _sent_order(client, supplier_payload)
    cancelled = (await client.post(f"/purchase-orders/{number}/cancel", json={
        "reason": "price increase rejected, sourcing elsewhere",
    })).json()
    assert cancelled["status"] == "cancelled"
    assert cancelled["cancel_reason"].startswith("price increase rejected")

    # A cancelled order is final: nothing further may be done to it.
    assert (await client.post(f"/purchase-orders/{number}/send")).status_code == 409


async def test_a_received_order_cannot_be_revised(client, supplier_payload):
    supplier, number, _ = await _sent_order(client, supplier_payload)
    await client.post("/inbound/email", json={
        "external_id": "msg-ack-final", "from_address": supplier["email"],
        "subject": f"Re: {number}", "body": f"We confirm {number}.",
    })
    await client.post(f"/purchase-orders/{number}/receive", json={"received": {}})
    blocked = await client.post(f"/purchase-orders/{number}/revise", json={
        "lines": [{"sku": "ALN-SILK-NVY", "quantity": 1, "unit_price": "1.00"}],
        "reason": "too late",
    })
    assert blocked.status_code == 409


async def test_booking_goods_in_writes_to_nobody(client, supplier_payload, monkeypatch):
    """Found in production: three receipt emails reached a supplier that nobody
    had pressed send for. Counting stock onto a shelf is a warehouse act, and it
    must not put mail in somebody else's inbox as a side effect of being honest
    about what arrived."""
    from sca.mail.base import ConsoleMailer

    postbox = ConsoleMailer()
    monkeypatch.setattr("sca.orders.service.get_mailer", lambda settings: postbox)

    supplier, number, _ = await _sent_order(client, supplier_payload)
    await client.post("/inbound/email", json={
        "external_id": "msg-ack-quiet", "from_address": supplier["email"],
        "subject": f"Re: {number}", "body": f"We confirm {number}.",
    })
    sent_before = len(postbox.sent)
    received = await client.post(f"/purchase-orders/{number}/receive", json={"received": {}})
    assert received.json()["status"] == "received"
    assert len(postbox.sent) == sent_before, "receiving must not send mail"

    # And the note is still sendable, by somebody who asked for it.
    note = await client.post(f"/purchase-orders/{number}/receipt-note")
    assert note.status_code == 200
    assert note.json()["delivered"] is True
    assert len(postbox.sent) == sent_before + 1
    assert number in postbox.sent[-1].subject


async def test_a_receipt_note_cannot_be_sent_before_the_goods_arrive(client, supplier_payload):
    _, number, _ = await _sent_order(client, supplier_payload)
    refused = await client.post(f"/purchase-orders/{number}/receipt-note")
    assert refused.status_code == 409
    assert "not been received" in refused.json()["detail"]


async def test_the_supplier_message_names_their_timezone_and_the_revision(
    client, supplier_payload
):
    """A buyer has to be able to read the exact text before it goes anywhere."""
    supplier, number, _ = await _sent_order(client, supplier_payload)
    first = (await client.get(f"/purchase-orders/{number}/message")).json()
    assert first["to"] == supplier["email"]
    assert number in first["subject"]
    assert "Rev" not in first["subject"]
    # The deadline is stated in the supplier's own zone, named, or it is ambiguous.
    assert "working hours" in first["body"]
    assert supplier["name"] in first["body"]

    detail = (await client.get(f"/purchase-orders/{number}")).json()
    line = detail["lines"][0]
    await client.post(f"/purchase-orders/{number}/revise", json={
        "lines": [{"sku": line["sku"], "quantity": line["quantity"], "unit_price": "40.00"}],
        "reason": "held to the agreed price",
    })
    revised = (await client.get(f"/purchase-orders/{number}/message")).json()
    assert "Rev 1" in revised["subject"]
    assert "held to the agreed price" in revised["body"]
    assert "replaces our earlier" in revised["body"]


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


# --------------------------------------------------------- the buyer's own hand
# The forecast proposes and a person decides. What is pinned here is that the
# decision survives the trip: the typed quantity is what gets ordered, the size
# curve reaches the mill, and a curve that disagrees with its line is refused
# rather than sent for the supplier to choose between.


async def _low(client, supplier_payload):
    await _setup(client, supplier_payload)
    await client.post("/stock", json={
        "sku": "ALN-SILK-NVY", "on_hand": 260, "on_order": 0, "weekly_forecast": "90",
    })


async def test_a_typed_quantity_beats_the_suggestion(client, supplier_payload):
    await _low(client, supplier_payload)
    created = (await client.post("/planning/create-orders", json={
        "skus": ["ALN-SILK-NVY"], "quantity_by_sku": {"ALN-SILK-NVY": 120},
    })).json()
    number = created["orders"][0]["number"]
    order = (await client.get(f"/purchase-orders/{number}")).json()
    # 500 was suggested; the buyer knows something the forecast does not.
    assert order["lines"][0]["quantity"] == 120
    assert order["total_value"] == "5040.00"


async def test_zero_is_a_decision_not_to_buy(client, supplier_payload):
    await _low(client, supplier_payload)
    created = (await client.post("/planning/create-orders", json={
        "skus": ["ALN-SILK-NVY"], "quantity_by_sku": {"ALN-SILK-NVY": 0},
    })).json()
    # Not an order for nothing, and not the suggestion either.
    assert created["created"] == 0


async def test_the_size_curve_reaches_the_mill(client, supplier_payload):
    await _low(client, supplier_payload)
    curve = {"S": 15, "M": 45, "L": 40, "XL": 15, "XXL": 5}
    created = (await client.post("/planning/create-orders", json={
        "skus": ["ALN-SILK-NVY"], "quantity_by_sku": {"ALN-SILK-NVY": 120},
        "sizes_by_sku": {"ALN-SILK-NVY": curve},
    })).json()
    number = created["orders"][0]["number"]
    order = (await client.get(f"/purchase-orders/{number}")).json()
    assert order["lines"][0]["sizes"] == curve

    # And it is in the words the supplier actually receives, not only in the row.
    message = (await client.get(f"/purchase-orders/{number}/message")).json()
    body = message.get("body") or message.get("text") or ""
    assert "M x 45" in body and "XXL x 5" in body


async def test_a_curve_that_does_not_add_up_is_refused(client, supplier_payload):
    await _low(client, supplier_payload)
    refused = await client.post("/planning/create-orders", json={
        "skus": ["ALN-SILK-NVY"], "quantity_by_sku": {"ALN-SILK-NVY": 100},
        "sizes_by_sku": {"ALN-SILK-NVY": {"M": 45, "L": 40}},
    })
    # Two different numbers on one line lets the mill choose which to cut.
    assert refused.status_code == 422
    assert "85" in refused.json()["detail"] and "100" in refused.json()["detail"]
    assert (await client.get("/purchase-orders")).json() == []


async def test_no_curve_stays_absent_rather_than_becoming_an_even_split(
    client, supplier_payload
):
    await _low(client, supplier_payload)
    created = (await client.post("/planning/create-orders", json={
        "skus": ["ALN-SILK-NVY"],
    })).json()
    number = created["orders"][0]["number"]
    order = (await client.get(f"/purchase-orders/{number}")).json()
    assert order["lines"][0]["sizes"] is None


async def test_a_supplier_minimum_is_shown_not_ordered(client, supplier_payload):
    """The mill's minimum is a fact about them, not about the demand.

    Folding it into the suggestion turned "you need 316" into "buy 2,000" with
    nothing on screen saying which was the need — a year of stock proposed as
    though the forecast had asked for it. It still decides who is cheapest,
    which is the comparison it belongs in; it no longer becomes the order.
    """
    await _setup(client, supplier_payload, moq=2000, pack_size=500,
                 unit_cost="3.20")
    await client.post("/stock", json={
        "sku": "ALN-SILK-NVY", "on_hand": 180, "on_order": 0, "weekly_forecast": "39",
    })
    line = (await client.post("/planning/suggest")).json()["by_supplier"][0]["lines"][0]

    # Enough for the cover target on its pack size, not the mill's 2,000.
    assert line["suggest_quantity"] < 2000
    assert line["suggest_quantity"] % 500 == 0
    assert line["supplier_moq"] == 2000
    assert line["below_supplier_minimum"] is True

    # And the order raised carries the need, so the mill is asked for what is
    # wanted rather than sent a number nobody chose.
    created = (await client.post("/planning/create-orders", json={
        "skus": ["ALN-SILK-NVY"],
    })).json()
    order = (await client.get(f"/purchase-orders/{created['orders'][0]['number']}")).json()
    assert order["lines"][0]["quantity"] == line["suggest_quantity"]
