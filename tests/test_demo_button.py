"""The demo button, which is pressed in front of people.

It used to invent a product. A fabricated SKU has no customers behind it, so the
line it produced could only be planned from a typed forecast — the button
demonstrated the weakest part of the system and left a fake row in the catalogue
afterwards. These pin the replacement: it draws down something real, and it
refuses rather than inventing when there is nothing real to draw down.
"""

from datetime import UTC, datetime, timedelta

from cdp.models.event import Event
from cdp.models.person import Person
from sca.models import Item, StockSnapshot, Supplier


async def _catalogue(session) -> str:
    supplier = Supplier(code="SUP1", name="Mill", email="mill@example.com", lead_time_days=21)
    session.add(supplier)
    await session.flush()
    for sku, on_hand in (("ALN-SILK-NVY", 4000), ("RWS-LIP-TUBE", 400)):
        session.add(Item(sku=sku, name=sku, supplier_id=supplier.id, unit_cost=10))
        session.add(StockSnapshot(sku=sku, on_hand=on_hand, on_order=0, weekly_forecast=0))
    await session.flush()
    return supplier.id


async def _sales(session, *, sku: str, quantity: int, count: int) -> None:
    person = Person(display_name="Buyer")
    session.add(person)
    await session.flush()
    now = datetime.now(UTC)
    for index in range(count):
        session.add(
            Event(
                person_id=person.id,
                source="shopify",
                name="order_paid",
                occurred_at=now - timedelta(days=3 + index * 4),
                payload={"line_items": [{"sku": sku, "quantity": quantity}]},
            )
        )
    await session.flush()


async def test_draws_down_a_real_item_rather_than_inventing_one(client, session):
    await _catalogue(session)
    await _sales(session, sku="ALN-SILK-NVY", quantity=40, count=6)
    await session.commit()

    response = await client.post("/demo/sample-data")

    assert response.status_code == 201
    body = response.json()
    assert body["written"][0]["sku"] == "ALN-SILK-NVY"
    assert body["weeks_cover"] == 2.5
    assert body["was_weeks_cover"] > 2.5

    items = (await client.get("/items")).json()
    # Nothing was created: the catalogue is the same size it started.
    assert len(items) == 2
    drawn = next(i for i in items if i["sku"] == "ALN-SILK-NVY")
    assert drawn["weeks_cover"] <= 2.6
    # And the line it produced is planned from sales, not from a typed number.
    assert drawn["forecast_source"] == "sales"

    plan = (await client.post("/planning/suggest")).json()
    assert [line["sku"] for group in plan["by_supplier"] for line in group["lines"]] == [
        "ALN-SILK-NVY"
    ]


async def test_picks_the_item_with_the_most_headroom(client, session):
    """Repeated presses should walk the catalogue, not fight over one row."""
    await _catalogue(session)
    # Equal weekly demand, very different cover: 4000 on hand against 400.
    await _sales(session, sku="ALN-SILK-NVY", quantity=40, count=6)
    await _sales(session, sku="RWS-LIP-TUBE", quantity=40, count=6)
    await session.commit()

    first = (await client.post("/demo/sample-data")).json()
    second = (await client.post("/demo/sample-data")).json()

    assert first["written"][0]["sku"] == "ALN-SILK-NVY"
    assert second["written"][0]["sku"] == "RWS-LIP-TUBE"


async def test_refuses_when_nothing_real_has_sales_behind_it(client, session):
    """Better a clear refusal than a fabricated product nobody ever bought."""
    await _catalogue(session)
    await session.commit()

    response = await client.post("/demo/sample-data")

    assert response.status_code == 409
    assert "sales history" in response.json()["detail"]
