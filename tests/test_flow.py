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


async def test_a_whatsapp_supplier_is_reached_on_whatsapp(
    client, supplier_payload, monkeypatch
):
    """The supplier's own channel, not the one the system happens to have wired.

    The template carries the order's shape and not the order — Meta caps the body
    and a line table does not fit — but the letter is still filed in full, so the
    record does not part company with what was sent at the moment it matters.
    """
    from sca.whatsapp.base import ConsoleSender

    phone = ConsoleSender()
    monkeypatch.setattr("sca.orders.service.get_sender", lambda settings: phone)

    supplier = await _setup(client, supplier_payload)
    await client.post("/suppliers", json=supplier_payload | {
        "working_days": "1,2,3,4,5,6,7", "work_start_hour": 0, "work_end_hour": 24,
        "channel": "whatsapp", "phone": "+91 82209 58384", "country": "IN",
    })
    await client.post("/stock", json={
        "sku": "ALN-SILK-NVY", "on_hand": 260, "on_order": 0, "weekly_forecast": "90",
    })
    created = (await client.post("/planning/create-orders")).json()
    number = created["orders"][0]["number"]
    await client.post(f"/purchase-orders/{number}/approve", json={"approver": "lead"})
    sent = await client.post(f"/purchase-orders/{number}/send")
    assert sent.status_code == 200

    assert len(phone.sent) == 1
    message = phone.sent[0]
    assert message.to == "918220958384", "typed with spaces, sent as E.164"
    assert message.template == "purchase_order"
    assert message.variables[0] == number
    assert message.variables[1] == "1", "one line"
    assert message.variables[3] == supplier["currency"]

    # And the letter itself is in the record even though the wire carried a
    # template, because that is what goes out when they reply.
    thread = (await client.get(f"/purchase-orders/{number}/thread")).json()
    ours = [e for e in thread["entries"] if e["side"] == "ours"]
    assert len(ours) == 1
    assert "ALN-SILK-NVY" in ours[0]["body"], "the full order, not the template"
    assert ours[0]["delivered"] is True


async def test_a_whatsapp_supplier_with_no_number_is_refused_not_guessed(
    client, supplier_payload, monkeypatch
):
    """A send that cannot be addressed must stop the order, exactly as a bounced
    email does. An order marked sent that never left is the one failure this
    system exists to prevent."""
    from sca.whatsapp.base import ConsoleSender

    monkeypatch.setattr("sca.orders.service.get_sender", lambda settings: ConsoleSender())
    await _setup(client, supplier_payload)
    await client.post("/suppliers", json=supplier_payload | {
        "working_days": "1,2,3,4,5,6,7", "work_start_hour": 0, "work_end_hour": 24,
        "channel": "whatsapp", "phone": None,
    })
    await client.post("/stock", json={
        "sku": "ALN-SILK-NVY", "on_hand": 260, "on_order": 0, "weekly_forecast": "90",
    })
    created = (await client.post("/planning/create-orders")).json()
    number = created["orders"][0]["number"]
    await client.post(f"/purchase-orders/{number}/approve", json={"approver": "lead"})

    refused = await client.post(f"/purchase-orders/{number}/send")
    assert refused.status_code == 409
    assert "no phone number" in refused.json()["detail"]
    detail = (await client.get(f"/purchase-orders/{number}")).json()
    assert detail["status"] == "approved", "still waiting to be sent"


async def _whatsapp_order(client, supplier_payload, phone="+91 82209 58384"):
    """An order sent on WhatsApp, ready to be written to. Returns its number."""
    await _setup(client, supplier_payload)
    await client.post("/suppliers", json=supplier_payload | {
        "working_days": "1,2,3,4,5,6,7", "work_start_hour": 0, "work_end_hour": 24,
        "channel": "whatsapp", "phone": phone, "country": "IN",
    })
    await client.post("/stock", json={
        "sku": "ALN-SILK-NVY", "on_hand": 260, "on_order": 0, "weekly_forecast": "90",
    })
    created = (await client.post("/planning/create-orders")).json()
    number = created["orders"][0]["number"]
    await client.post(f"/purchase-orders/{number}/approve", json={"approver": "lead"})
    await client.post(f"/purchase-orders/{number}/send")
    return number


async def _wrote_to_us(sessionmaker, phone, *, ago=timedelta(hours=1)):
    """A supplier's WhatsApp message, at a chosen distance in the past.

    Written straight to the table rather than pushed through the webhook: what
    is under test is the window the message opens, and going via Meta's envelope
    would leave the received time to whatever the fixture happened to encode.
    """
    from sqlalchemy import select

    from sca.models import InboundMessage

    digits = "".join(c for c in phone if c.isdigit())
    async with sessionmaker() as session:
        supplier = await session.scalar(select(Supplier).limit(1))
        session.add(InboundMessage(
            external_id=f"wamid.WINDOW-{ago.total_seconds():.0f}",
            source="whatsapp", from_address=digits, supplier_id=supplier.id,
            body="ok", received_at=datetime.now(UTC) - ago, kind="unknown",
        ))
        await session.commit()


async def test_free_text_is_refused_until_the_supplier_writes_first(
    client, supplier_payload, monkeypatch
):
    """Meta refuses a business's own words unless the other side wrote within the
    day. Discovering that at their edge means a message somebody composed, sent,
    and believed had arrived — so the refusal has to happen here, with the reason
    on it."""
    from sca.whatsapp.base import ConsoleSender

    phone = ConsoleSender()
    monkeypatch.setattr("sca.orders.service.get_sender", lambda settings: phone)

    number = await _whatsapp_order(client, supplier_payload)
    refused = await client.post(f"/purchase-orders/{number}/whatsapp/message",
                                json={"text": "any word on the silk?"})
    assert refused.status_code == 409
    assert "24 hours" in refused.json()["detail"]
    assert "never written" in refused.json()["detail"], "which of the two closed states"
    assert not [m for m in phone.sent if getattr(m, "text", None)], "nothing left"

    thread = (await client.get(f"/purchase-orders/{number}/thread")).json()
    assert thread["whatsapp"]["open"] is False
    assert thread["whatsapp"]["can_send"] is True, "the number is on file; the clock is not"


async def test_a_reply_opens_the_window_and_free_text_goes_out(
    client, supplier_payload, monkeypatch, sessionmaker_fixture
):
    from sca.whatsapp.base import ConsoleSender

    phone = ConsoleSender()
    monkeypatch.setattr("sca.orders.service.get_sender", lambda settings: phone)

    number = await _whatsapp_order(client, supplier_payload)
    await _wrote_to_us(sessionmaker_fixture, "+91 82209 58384")

    thread = (await client.get(f"/purchase-orders/{number}/thread")).json()
    assert thread["whatsapp"]["open"] is True
    assert thread["whatsapp"]["closes_at"], "and the drawer can say until when"

    out = await client.post(f"/purchase-orders/{number}/whatsapp/message",
                            json={"text": "any word on the silk?"})
    assert out.status_code == 200
    assert phone.sent[-1].text == "any word on the silk?"
    assert phone.sent[-1].to == "918220958384"

    # And it is in the record as free text, not as the template: the drawer marks
    # a template send with what the wire actually carried, which would be a lie
    # over words somebody typed.
    thread = (await client.get(f"/purchase-orders/{number}/thread")).json()
    ours = [e for e in thread["entries"] if e["side"] == "ours"]
    assert ours[-1]["body"] == "any word on the silk?"
    assert ours[-1]["channel"] == "whatsapp"
    assert ours[-1]["as_template"] is False


async def test_a_day_old_reply_does_not_still_hold_the_window_open(
    client, supplier_payload, monkeypatch, sessionmaker_fixture
):
    """The bound is 24 hours from their message, not from any message. A supplier
    who wrote yesterday reads as reachable on a thread that has been open all
    week, and the send fails at Meta rather than here."""
    from sca.whatsapp.base import ConsoleSender

    monkeypatch.setattr("sca.orders.service.get_sender", lambda settings: ConsoleSender())
    number = await _whatsapp_order(client, supplier_payload)
    await _wrote_to_us(sessionmaker_fixture, "+91 82209 58384", ago=timedelta(hours=25))

    refused = await client.post(f"/purchase-orders/{number}/whatsapp/message",
                                json={"text": "still waiting"})
    assert refused.status_code == 409
    assert "window has closed" in refused.json()["detail"]


async def test_an_empty_message_is_not_a_message(
    client, supplier_payload, monkeypatch, sessionmaker_fixture
):
    from sca.whatsapp.base import ConsoleSender

    monkeypatch.setattr("sca.orders.service.get_sender", lambda settings: ConsoleSender())
    number = await _whatsapp_order(client, supplier_payload)
    await _wrote_to_us(sessionmaker_fixture, "+91 82209 58384")

    empty = await client.post(f"/purchase-orders/{number}/whatsapp/message",
                              json={"text": "   "})
    assert empty.status_code == 422


async def test_a_file_we_send_is_kept_as_well_as_sent(
    client, supplier_payload, monkeypatch, sessionmaker_fixture
):
    """The copy is the point. A specification sent on WhatsApp is what an argument
    about the wrong goods turns on months later, and the supplier's phone is not a
    record this business controls."""
    from sca.whatsapp.base import ConsoleSender

    phone = ConsoleSender()
    monkeypatch.setattr("sca.orders.service.get_sender", lambda settings: phone)
    number = await _whatsapp_order(client, supplier_payload)
    await _wrote_to_us(sessionmaker_fixture, "+91 82209 58384")

    out = await client.post(
        f"/purchase-orders/{number}/whatsapp/file",
        files={"file": ("spec.pdf", b"%PDF-1.4 the curve", "application/pdf")},
        data={"caption": "the curve, as agreed"},
    )
    assert out.status_code == 200
    assert phone.sent[-1].filename == "spec.pdf"
    assert phone.sent[-1].kind == "document", "a PDF keeps its name; an image would not"
    assert phone.sent[-1].caption == "the curve, as agreed"

    thread = (await client.get(f"/purchase-orders/{number}/thread")).json()
    ours = [e for e in thread["entries"] if e["side"] == "ours"][-1]
    assert ours["body"] == "the curve, as agreed", "the caption is the message"
    assert [f["filename"] for f in ours["files"]] == ["spec.pdf"]

    # And the bytes come back exactly, from the same route the supplier's own
    # files are served on.
    got = await client.get(f"/inbound/attachments/{ours['files'][0]['id']}")
    assert got.status_code == 200
    assert got.content == b"%PDF-1.4 the curve"


async def test_a_file_cannot_be_sent_outside_the_window_either(
    client, supplier_payload, monkeypatch, sessionmaker_fixture
):
    """The rule is about the conversation, not about what is put into it. A file
    that skipped the check would fail at Meta after the upload had already
    happened, leaving bytes in their store and nothing here to explain it."""
    from sca.whatsapp.base import ConsoleSender

    phone = ConsoleSender()
    monkeypatch.setattr("sca.orders.service.get_sender", lambda settings: phone)
    number = await _whatsapp_order(client, supplier_payload)

    refused = await client.post(
        f"/purchase-orders/{number}/whatsapp/file",
        files={"file": ("spec.pdf", b"%PDF", "application/pdf")},
    )
    assert refused.status_code == 409
    # The order's own template went out earlier and is in here; the file is not.
    assert not [m for m in phone.sent if getattr(m, "filename", None)], "nothing uploaded"


async def test_an_empty_file_is_refused_before_it_is_uploaded(
    client, supplier_payload, monkeypatch, sessionmaker_fixture
):
    from sca.whatsapp.base import ConsoleSender

    monkeypatch.setattr("sca.orders.service.get_sender", lambda settings: ConsoleSender())
    number = await _whatsapp_order(client, supplier_payload)
    await _wrote_to_us(sessionmaker_fixture, "+91 82209 58384")

    empty = await client.post(
        f"/purchase-orders/{number}/whatsapp/file",
        files={"file": ("nothing.pdf", b"", "application/pdf")},
    )
    assert empty.status_code == 422


async def test_the_thread_holds_both_halves_of_the_exchange(
    client, supplier_payload, monkeypatch
):
    """Replies were stored from the beginning and our own letters were not, so
    the record was one-sided: a supplier could be answering figures nobody here
    could still produce."""
    from sca.mail.base import ConsoleMailer

    monkeypatch.setattr("sca.orders.service.get_mailer", lambda settings: ConsoleMailer())
    supplier, number, _ = await _sent_order(client, supplier_payload)
    await client.post("/inbound/email", json={
        "external_id": "msg-thread-1", "from_address": supplier["email"],
        "subject": f"Re: {number}", "body": f"We confirm {number}.",
    })

    thread = (await client.get(f"/purchase-orders/{number}/thread")).json()
    sides = [e["side"] for e in thread["entries"]]
    assert sides == ["ours", "theirs"], "oldest first, both directions"
    assert thread["entries"][0]["delivered"] is True
    assert number in thread["entries"][0]["body"]
    assert "We confirm" in thread["entries"][1]["body"]
    assert thread["not_kept"] == 0


async def test_what_needs_a_person_is_on_the_order_it_belongs_to(
    client, supplier_payload
):
    """The row used to carry a bare "issue" tag: something is wrong, and nothing
    about what. An exception is nearly always about the correspondence, so it
    belongs beside it — with the move it suggests, not just the alarm."""
    supplier, number, _ = await _sent_order(client, supplier_payload)
    await client.post("/inbound/email", json={
        "external_id": "msg-issue-1", "from_address": supplier["email"],
        "subject": f"Re: {number}", "body": f"We confirm {number}.",
    })
    received = await client.post(f"/purchase-orders/{number}/receive",
                                 json={"received": {"ALN-SILK-NVY": 450}})
    assert received.json()["issues_raised"], "a short delivery raises one"

    thread = (await client.get(f"/purchase-orders/{number}/thread")).json()
    assert len(thread["issues"]) == 1
    issue = thread["issues"][0]
    assert issue["kind"] == "short_shipment"
    assert "450 received of 500" in issue["detail"]
    assert issue["suggested_action"], "an alarm with no move is where it started"


async def test_a_letter_we_did_not_keep_is_recovered_from_the_reply_that_quoted_it(
    client, supplier_payload, monkeypatch
):
    """The supplier's own copy, quoted back. Not a reconstruction: recomposing
    from today's rows would show figures that were never sent, but a reply
    quoting our message is evidence of what they received — labelled as theirs,
    because their mail client may have rewrapped it."""
    from sca.api.orders import _letter_inside

    recovered = _letter_inside(
        "I accept this order.\n"
        "\n"
        "On Tue, 11 Aug, 2026, 3:46 pm Procurement, <buyer@example.com>\n"
        "wrote:\n"
        "\n"
        "> Dear Asfer,\n"
        ">\n"
        ">   ALN-SILK-NVY   500 x 40.00 = 20,000.00\n"
    )
    assert recovered is not None
    assert recovered.startswith("Dear Asfer,")
    assert "500 x 40.00" in recovered
    assert ">" not in recovered, "the quote marks are their client, not our words"
    assert "I accept this order" not in recovered, "their words are not ours"

    # A reply with nothing quoted recovers nothing rather than guessing.
    assert _letter_inside("confirmed, thanks") is None
    assert _letter_inside("") is None


async def test_a_revision_does_not_rewrite_the_letter_already_sent(
    client, supplier_payload, monkeypatch
):
    """The whole reason the text is stored rather than recomposed. A supplier
    answered one set of figures; showing them today's under that date would read
    as evidence of something that never happened."""
    from sca.mail.base import ConsoleMailer

    monkeypatch.setattr("sca.orders.service.get_mailer", lambda settings: ConsoleMailer())
    _, number, _ = await _sent_order(client, supplier_payload)
    await client.post(f"/purchase-orders/{number}/revise", json={
        "lines": [{"sku": "ALN-SILK-NVY", "quantity": 999, "unit_price": "40.00"}],
        "reason": "counter offer",
    })
    await client.post(f"/purchase-orders/{number}/approve", json={"approver": "lead"})

    thread = (await client.get(f"/purchase-orders/{number}/thread")).json()
    first = thread["entries"][0]
    assert "999" not in first["body"], "the sent letter must not follow the order"
    assert first["revision"] == 0


async def test_a_send_that_failed_leaves_neither_a_letter_nor_a_sent_order(
    client, supplier_payload, monkeypatch
):
    """The two have to agree. A letter in the record beside an order still
    sitting in approval would read as "they were told" when nobody was."""
    from sca.mail.base import MailError

    class Broken:
        name = "smtp"

        async def deliver(self, message):
            raise MailError("SMTP delivery failed: [Errno 101] Network is unreachable")

    created = await _setup(client, supplier_payload)
    order = (await client.post("/purchase-orders", json={
        "supplier_id": created["id"],
        "lines": [{"sku": "ALN-SILK-NVY", "quantity": 10, "unit_price": "40.00"}],
    })).json()
    number = order["number"]
    await client.post(f"/purchase-orders/{number}/approve", json={"approver": "lead"})

    monkeypatch.setattr("sca.orders.service.get_mailer", lambda settings: Broken())
    refused = await client.post(f"/purchase-orders/{number}/send")
    assert refused.status_code == 409
    assert "unreachable" in refused.json()["detail"]

    thread = (await client.get(f"/purchase-orders/{number}/thread")).json()
    assert thread["entries"] == []
    assert thread["not_kept"] == 0
    assert thread["status"] == "approved", "still waiting to be sent"


async def test_the_preview_reads_back_what_is_being_typed(client, supplier_payload):
    """Found in production: the preview raised inside the composer because the
    stand-in lines it builds had no size attribute, the dialog caught it, and the
    previous draft stayed on screen looking current. A buyer read a letter for
    ten units while saving fifteen."""
    _, number, _ = await _sent_order(client, supplier_payload)
    preview = await client.post(f"/purchase-orders/{number}/message", json={
        "lines": [{"sku": "ALN-SILK-NVY", "quantity": 20, "unit_price": "40.00"}],
        "reason": "counter offer",
    })
    assert preview.status_code == 200
    body = preview.json()["body"]
    assert "20 x" in body
    assert "800.00" in body
    assert "counter offer" in body

    # And a curve typed alongside it reaches the mill in the same letter.
    with_sizes = await client.post(f"/purchase-orders/{number}/message", json={
        "lines": [{
            "sku": "ALN-SILK-NVY", "quantity": 20, "unit_price": "40.00",
            "sizes": {"L": 12, "S": 8},
        }],
        "reason": "counter offer",
    })
    assert "sizes: L x 12  S x 8" in with_sizes.json()["body"]


async def test_a_revision_carries_the_size_split_it_was_given(client, supplier_payload):
    """A revision rebuilds every line from what the caller sends, so a split left
    out of that payload is a split deleted from the order — silently, on an order
    the mill has already been told the sizes for."""
    _, number, _ = await _sent_order(client, supplier_payload)
    revised = await client.post(f"/purchase-orders/{number}/revise", json={
        "lines": [{
            "sku": "ALN-SILK-NVY", "quantity": 30, "unit_price": "40.00",
            "sizes": {"L": 18, "S": 12},
        }],
        "reason": "counter offer",
    })
    assert revised.status_code == 200
    assert revised.json()["lines"][0]["sizes"] == {"L": 18, "S": 12}

    # And a split that does not add up to the line is refused, not sent.
    refused = await client.post(f"/purchase-orders/{number}/revise", json={
        "lines": [{
            "sku": "ALN-SILK-NVY", "quantity": 30, "unit_price": "40.00",
            "sizes": {"L": 18, "S": 5},
        }],
        "reason": "counter offer",
    })
    assert refused.status_code == 422
    assert "add to 23" in refused.json()["detail"]


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
