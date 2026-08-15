"""Sales rung up in the shop.

The point of this screen is that one action writes two records — the demand and
the stock — and that the second one is what makes the first one measurable. So
what is pinned here is mostly the ways that pairing can quietly come apart: a
double press that empties a shelf twice, a walk-in basket that mints a customer
per sale, a count that disagrees with the shelf, and a price field that could
have been filled in with our own cost.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from cdp.models.event import Event
from cdp.models.person import Person
from sca.models import Item, StockLevel, StockSnapshot, Supplier


@pytest.fixture
async def shop(client, session):
    """One supplier, two items, a known shelf."""
    supplier = Supplier(code="ALN", name="Aleena Atelier")
    session.add(supplier)
    await session.flush()
    session.add_all([
        Item(sku="ALN-ABAYA-01", name="Everyday abaya", supplier_id=supplier.id, brand="aleena"),
        Item(sku="ALN-SCARF-02", name="Silk scarf", supplier_id=supplier.id, brand="aleena"),
    ])
    session.add(StockSnapshot(sku="ALN-ABAYA-01", on_hand=10, on_order=0))
    session.add(StockSnapshot(sku="ALN-SCARF-02", on_hand=4, on_order=0))
    await session.commit()
    return supplier


async def test_a_sale_takes_the_stock_off_the_shelf(client, session, shop):
    out = (await client.post("/sales", json={
        "location": "riyadh",
        "receipt": "r-1",
        "lines": [{"sku": "ALN-ABAYA-01", "quantity": 3, "unit_price": "450.00"}],
    })).json()

    assert out["accepted"] and not out["duplicate"]
    assert out["units"] == 3
    assert out["value"] == "1350.00"
    assert out["stock"] == [{"sku": "ALN-ABAYA-01", "sold": 3, "was": 10, "now": 7}]

    snapshot = await session.get(StockSnapshot, "ALN-ABAYA-01")
    await session.refresh(snapshot)
    assert snapshot.on_hand == 7


async def test_the_movement_is_appended_to_the_ledger(client, session, shop):
    """The whole reason the screen exists. Demand is divided by the weeks an item
    was sellable, and that divisor is read from these rows — a sale that moved
    stock without leaving a reading would be counted in the numerator and be
    invisible in the denominator."""
    await client.post("/sales", json={
        "location": "riyadh",
        "receipt": "r-1", "lines": [{"sku": "ALN-ABAYA-01", "quantity": 2}],
    })

    rows = list(await session.scalars(
        select(StockLevel)
        .where(StockLevel.sku == "ALN-ABAYA-01")
        .where(StockLevel.location_code.is_(None))
    ))
    assert [r.on_hand for r in rows] == [8]


async def test_the_sale_is_readable_as_demand(client, session, shop):
    """The payload has to speak the vocabulary ``planning.demand`` reads, or the
    shop's sales land in the database and never reach the buying desk."""
    from sca.planning.demand import weekly_demand

    await client.post("/sales", json={
        "location": "riyadh",
        "receipt": "r-1",
        "lines": [{"sku": "ALN-ABAYA-01", "quantity": 6, "unit_price": "450.00"}],
    })
    measured = await weekly_demand(session, now=datetime.now(UTC) + timedelta(hours=1))
    assert "ALN-ABAYA-01" in measured
    assert measured["ALN-ABAYA-01"].units == 6


async def test_pressing_save_twice_records_one_sale(client, session, shop):
    """The failure this is guarding is not a duplicate row, it is a shelf emptied
    twice by one basket — which then reads as a stockout nobody had."""
    body = {"receipt": "same-receipt", "location": "riyadh",
            "lines": [{"sku": "ALN-ABAYA-01", "quantity": 3}]}
    first = (await client.post("/sales", json=body)).json()
    second = (await client.post("/sales", json=body)).json()

    assert not first["duplicate"] and second["duplicate"]
    assert second["stock"] == []
    snapshot = await session.get(StockSnapshot, "ALN-ABAYA-01")
    await session.refresh(snapshot)
    assert snapshot.on_hand == 7

    events = await session.scalar(
        select(func.count(Event.id)).where(Event.source == "pos")
    )
    assert events == 1


async def test_walk_in_sales_share_one_counter_record(client, session, shop):
    """Anonymous baskets have to go somewhere, or the items in them vanish from
    demand. Somewhere is one standing record per till — a person per sale would
    turn a week of trading into several hundred customers who bought once."""
    for n in range(3):
        await client.post("/sales", json={
            "location": "riyadh",
            "receipt": f"r-{n}", "lines": [{"sku": "ALN-SCARF-02", "quantity": 1}],
        })

    people = list(await session.scalars(select(Person)))
    assert len(people) == 1
    assert people[0].synthetic is True
    assert people[0].display_name == "Walk-in — counter"


async def test_the_counter_is_kept_out_of_the_customer_list(client, session, shop):
    """It is not a customer. Left unmarked it would be the most frequent buyer in
    the business and would top every segment and every audience."""
    await client.post("/sales", json={
        "location": "riyadh",
        "receipt": "r-1", "lines": [{"sku": "ALN-SCARF-02", "quantity": 1}],
    })
    listed = (await client.get("/persons")).json()
    assert listed == []


async def test_a_phone_attaches_the_sale_to_a_customer(client, session, shop):
    out = (await client.post("/sales", json={
        "location": "riyadh",
        "receipt": "r-1", "phone": "0501234567", "name": "Noura",
        "lines": [{"sku": "ALN-ABAYA-01", "quantity": 1, "unit_price": "450.00"}],
    })).json()

    assert out["identified"] is True
    person = await session.get(Person, out["person_id"])
    assert person.synthetic is False
    assert person.display_name == "Noura"
    # She is a customer, so she belongs in the list the counter is kept out of.
    assert [p["person_id"] for p in (await client.get("/persons")).json()] == [person.id]


async def test_an_identified_sale_never_carries_the_counter_identifier(client, session, shop):
    """The contamination case. If a basket sent both her phone and the till, the
    till would be weak evidence linking her to the standing record, the record
    would inherit her number, and every walk-in afterwards would resolve to her.
    """
    await client.post("/sales", json={
        "location": "riyadh",
        "receipt": "r-1", "lines": [{"sku": "ALN-SCARF-02", "quantity": 1}],
    })
    await client.post("/sales", json={
        "location": "riyadh",
        "receipt": "r-2", "phone": "0501234567",
        "lines": [{"sku": "ALN-SCARF-02", "quantity": 1}],
    })
    await client.post("/sales", json={
        "location": "riyadh",
        "receipt": "r-3", "lines": [{"sku": "ALN-SCARF-02", "quantity": 1}],
    })

    people = list(await session.scalars(select(Person)))
    counters = [p for p in people if p.synthetic]
    assert len(counters) == 1
    # The walk-ins are still anonymous, and she still owns her own record.
    assert len([p for p in people if not p.synthetic]) == 1


async def test_selling_more_than_the_record_holds_at_zero_and_says_so(client, session, shop):
    """A negative on-hand reads downstream as "not sellable", which shrinks the
    divisor in the demand calculation and inflates the figure — the shelf would
    end up buying more of itself off the back of a bad count."""
    out = (await client.post("/sales", json={
        "location": "riyadh",
        "receipt": "r-1", "lines": [{"sku": "ALN-SCARF-02", "quantity": 9}],
    })).json()

    assert out["stock"] == [{"sku": "ALN-SCARF-02", "sold": 9, "was": 4, "now": 0}]
    assert any("only 4 were on that shelf" in note for note in out["notes"])
    snapshot = await session.get(StockSnapshot, "ALN-SCARF-02")
    await session.refresh(snapshot)
    assert snapshot.on_hand == 0


async def test_a_line_with_no_price_is_demand_but_not_revenue(client, session, shop):
    """Never defaulted to the item's unit cost: that is what it cost us, and
    writing it here would file our own cost as the customer's payment."""
    out = (await client.post("/sales", json={
        "location": "riyadh",
        "receipt": "r-1",
        "lines": [
            {"sku": "ALN-ABAYA-01", "quantity": 2, "unit_price": "450.00"},
            {"sku": "ALN-SCARF-02", "quantity": 1},
        ],
    })).json()

    assert out["units"] == 3
    assert out["value"] == "900.00"
    assert any("No price on ALN-SCARF-02" in note for note in out["notes"])


async def test_the_same_sku_twice_in_one_basket_is_added_up(client, session, shop):
    """Two lines for one item is a legitimate way to ring up a basket, and it
    used to make the stock arithmetic read twice from a position it had already
    changed."""
    out = (await client.post("/sales", json={
        "location": "riyadh",
        "receipt": "r-1",
        "lines": [
            {"sku": "ALN-ABAYA-01", "quantity": 2},
            {"sku": "ALN-ABAYA-01", "quantity": 3},
        ],
    })).json()

    assert out["stock"] == [{"sku": "ALN-ABAYA-01", "sold": 5, "was": 10, "now": 5}]


async def test_an_unknown_sku_is_refused(client, shop):
    """It would appear in demand as a product that does not exist, and would be
    bought from a supplier who has never heard of it."""
    res = await client.post("/sales", json={
        "location": "riyadh",
        "receipt": "r-1", "lines": [{"sku": "NOPE-01", "quantity": 1}],
    })
    assert res.status_code == 422
    assert "NOPE-01" in res.json()["detail"]


async def test_recent_sales_lists_only_what_was_rung_up_here(client, session, shop):
    online = Person(display_name="Online buyer")
    session.add(online)
    await session.flush()
    session.add(Event(
        person_id=online.id, source="shopify", name="order_paid",
        occurred_at=datetime.now(UTC),
        payload={"line_items": [{"sku": "ALN-ABAYA-01", "quantity": 1}]},
    ))
    await session.commit()
    await client.post("/sales", json={
        "location": "riyadh",
        "receipt": "r-1", "lines": [{"sku": "ALN-ABAYA-01", "quantity": 2}],
    })

    rows = (await client.get("/sales")).json()
    assert len(rows) == 1
    assert rows[0]["receipt"] == "r-1"
    assert rows[0]["units"] == 2
    assert rows[0]["anonymous"] is True


async def test_a_sale_recorded_from_the_customer_console_reaches_demand(client, session, shop):
    """The gap this replaced. The touchpoint form used to post a typed product
    name and no SKU, so the sale landed on her profile perfectly and reached the
    shelf as nothing at all — nothing was ever reordered for it."""
    from sca.planning.demand import weekly_demand

    out = (await client.post("/sales", json={
        "receipt": "r-console",
        # The shop's own till, not Shopify's. shopify_pos is an order rung up on
        # a Shopify terminal and reaching us through the connector; these
        # counters are independent of the storefront entirely.
        "source": "pos",
        "channel": "retail",
        "location": "riyadh",
        "phone": "0501234567",
        "lines": [{"sku": "ALN-ABAYA-01", "quantity": 2, "unit_price": "450.00"}],
    })).json()
    assert out["identified"] is True

    measured = await weekly_demand(session, now=datetime.now(UTC) + timedelta(hours=1))
    assert measured["ALN-ABAYA-01"].units == 2

    events = list(await session.scalars(select(Event).where(Event.source == "pos")))
    assert len(events) == 1
    assert events[0].channel == "retail"


async def test_a_sale_can_be_recorded_without_moving_the_shelf(client, session, shop):
    """For a sale typed up after somebody has already counted the shelf down.
    Taking it off twice invents a stockout nobody had."""
    out = (await client.post("/sales", json={
        "location": "riyadh",
        "receipt": "r-late", "move_stock": False,
        "lines": [{"sku": "ALN-ABAYA-01", "quantity": 3}],
    })).json()

    assert out["stock"] == []
    assert any("Stock was not changed" in note for note in out["notes"])
    snapshot = await session.get(StockSnapshot, "ALN-ABAYA-01")
    await session.refresh(snapshot)
    assert snapshot.on_hand == 10


async def test_two_tills_ringing_walk_ins_at_once_share_one_record(client, session, shop):
    """The failure that killed a seeding run at three quarters through.

    Resolution reads the identifier table and then writes to it. Two anonymous
    sales arriving together both find no walk-in record, both create one, and the
    unique constraint on (kind, value) rejects the loser — correctly, because the
    alternative is two standing records for one till. A real shop with two
    counters does this every busy afternoon.

    What is pinned is the outcome, not the timing: however many anonymous baskets
    are rung up, there is exactly one walk-in record per till and every basket is
    accepted.
    """
    for _ in range(4):
        response = await client.post(
            "/sales",
            json={"lines": [{"sku": "ALN-ABAYA-01", "quantity": 1, "unit_price": "420.00"}],
                  "till": "counter", "location": "riyadh"},
        )
        assert response.status_code == 201, response.text
        assert response.json()["identified"] is False

    counters = await session.scalars(
        select(Person).where(Person.synthetic.is_(True))
    )
    assert len({p.id for p in counters}) == 1


async def test_a_lost_race_is_retried_once_and_no_further(session, monkeypatch):
    """The retry is deliberately not a loop.

    Losing the insert once is a race and the second attempt finds what the winner
    wrote. Losing it twice is a constraint that is genuinely violated, and a
    forever-retry would turn a bug into a hung request holding a transaction
    open.
    """
    from sqlalchemy.exc import IntegrityError

    from cdp.identity.service import IdentifierIn, IdentityService

    service = IdentityService(session, actor="test")
    real = service._resolve_once
    calls = {"n": 0}

    async def flaky(identifiers, *, seen_at):
        calls["n"] += 1
        if calls["n"] == 1:
            raise IntegrityError("insert", {}, Exception("duplicate key"))
        return await real(identifiers, seen_at=seen_at)

    monkeypatch.setattr(service, "_resolve_once", flaky)
    resolution = await service.resolve(
        [IdentifierIn("pos_counter", "counter:counter")], seen_at=datetime.now(UTC)
    )
    assert resolution.person_id
    assert calls["n"] == 2

    # And a second failure is allowed to surface rather than spin.
    always = {"n": 0}

    async def broken(identifiers, *, seen_at):
        always["n"] += 1
        raise IntegrityError("insert", {}, Exception("duplicate key"))

    monkeypatch.setattr(service, "_resolve_once", broken)
    with pytest.raises(IntegrityError):
        await service.resolve(
            [IdentifierIn("pos_counter", "counter:till-2")], seen_at=datetime.now(UTC)
        )
    assert always["n"] == 2


async def test_an_online_order_moves_the_same_shelf_a_counter_sale_does(client, session, shop):
    """One shelf, whoever sold from it.

    Shopify decremented its own count and this platform decremented nothing, so
    the two numbers began drifting apart at the first web order and never
    converged. The buying desk then planned against a shelf that had already
    emptied.
    """
    import json as _json

    from cdp.config import get_settings
    from tests.cdp.factories import signed

    # Signed with whatever secret this process actually loaded, not a constant
    # from another package's fixtures — settings are cached, so which one is in
    # force depends on what ran first.
    secret = get_settings().shopify_webhook_secret

    payload = {
        "id": 99001,
        "email": "noura@example.com",
        "total_price": "840.00",
        "currency": "SAR",
        "financial_status": "paid",
        "processed_at": "2026-08-01T10:00:00Z",
        "created_at": "2026-08-01T10:00:00Z",
        "updated_at": "2026-08-01T10:00:00Z",
        "customer": {"id": 5001, "email": "noura@example.com", "first_name": "Noura"},
        "line_items": [
            {"vendor": "Aleena", "sku": "ALN-ABAYA-01", "price": "420.00", "quantity": 2},
            # An item the catalogue has never heard of. The rest of the order
            # still lands: a webhook has nobody to correct, and refusing the
            # whole thing would lose the customer and the demand as well.
            {"vendor": "Aleena", "sku": "NOT-A-REAL-SKU", "price": "10.00", "quantity": 5},
        ],
    }
    body, mac = signed(secret, payload)
    headers = {
        "X-Shopify-Topic": "orders/paid",
        "X-Shopify-Hmac-Sha256": mac,
        "Content-Type": "application/json",
    }

    response = await client.post("/ingest/shopify", content=body, headers=headers)
    assert response.status_code == 200, response.text

    snapshot = await session.get(StockSnapshot, "ALN-ABAYA-01")
    await session.refresh(snapshot)
    assert snapshot.on_hand == 8, "the online sale did not come off the shelf"

    # And the ledger, which is what demand is divided by.
    rows = (
        await session.scalars(
            select(StockLevel)
            .where(StockLevel.sku == "ALN-ABAYA-01")
            .where(StockLevel.location_code.is_(None))
        )
    ).all()
    assert [r.on_hand for r in rows] == [8]

    # Shopify replays a topic whenever a 200 is slow. Twice off one shelf would
    # be a stockout nobody had.
    again = await client.post("/ingest/shopify", content=body, headers=headers)
    assert again.status_code == 200
    assert again.json()["duplicate"] is True
    await session.refresh(snapshot)
    assert snapshot.on_hand == 8

    assert _json.loads(body)["id"] == 99001


async def test_stock_is_held_per_shelf_and_the_total_follows(client, session, shop):
    """Twenty abayas is not a fact about the group.

    It is ten the website can ship and five in each shop, and only the first can
    be promised to somebody online. What is pinned here is that the two records
    move together: the shelf a sale came off, and the total buying reads.
    """
    from sca.models import StockAtLocation, StockLocation

    session.add_all([
        StockLocation(code="online", name="Shopify storefront", kind="online"),
        StockLocation(code="riyadh", name="Riyadh shop"),
        StockLocation(code="jeddah", name="Jeddah shop"),
    ])
    snapshot = await session.get(StockSnapshot, "ALN-ABAYA-01")
    snapshot.on_hand = 20
    session.add_all([
        StockAtLocation(sku="ALN-ABAYA-01", location_code="online", on_hand=10),
        StockAtLocation(sku="ALN-ABAYA-01", location_code="riyadh", on_hand=5),
        StockAtLocation(sku="ALN-ABAYA-01", location_code="jeddah", on_hand=5),
    ])
    await session.flush()

    response = await client.post(
        "/sales",
        json={"lines": [{"sku": "ALN-ABAYA-01", "quantity": 1, "unit_price": "420.00"}],
              "till": "counter", "location": "jeddah", "phone": "0551110000"},
    )
    assert response.status_code == 201, response.text

    await session.refresh(snapshot)
    assert snapshot.on_hand == 19, "the group's total did not follow the sale"

    shelves = {
        row.location_code: row.on_hand
        for row in await session.scalars(
            select(StockAtLocation).where(StockAtLocation.sku == "ALN-ABAYA-01")
        )
    }
    assert shelves == {"online": 10, "riyadh": 5, "jeddah": 4}, shelves

    # And the ledger keeps both readings: the group's, which demand is divided
    # by, and the shelf's.
    levels = (
        await session.scalars(
            select(StockLevel).where(StockLevel.sku == "ALN-ABAYA-01")
        )
    ).all()
    assert sorted(((r.location_code or ""), r.on_hand) for r in levels) == [
        ("", 19), ("jeddah", 4),
    ]


async def test_a_shop_selling_what_it_does_not_have_does_not_write_down_the_others(
    client, session, shop
):
    """A bad count in Jeddah is not a reason to lose stock in Riyadh.

    The group is short by what actually left the shelf, not by what the sale
    claimed — otherwise one miscounted shop quietly writes down the whole group
    and the buying desk orders against stock that was never missing.
    """
    from sca.models import StockAtLocation, StockLocation

    session.add_all([
        StockLocation(code="online", name="Shopify storefront", kind="online"),
        StockLocation(code="jeddah", name="Jeddah shop"),
    ])
    snapshot = await session.get(StockSnapshot, "ALN-ABAYA-01")
    snapshot.on_hand = 12
    session.add_all([
        StockAtLocation(sku="ALN-ABAYA-01", location_code="online", on_hand=10),
        StockAtLocation(sku="ALN-ABAYA-01", location_code="jeddah", on_hand=2),
    ])
    await session.flush()

    response = await client.post(
        "/sales",
        json={"lines": [{"sku": "ALN-ABAYA-01", "quantity": 5, "unit_price": "420.00"}],
              "location": "jeddah"},
    )
    assert response.status_code == 201, response.text
    out = response.json()
    assert any("count needs checking" in n for n in out["notes"]), out["notes"]

    await session.refresh(snapshot)
    # Two really left Jeddah, so the group is down two — not five.
    assert snapshot.on_hand == 10
    shelves = {
        row.location_code: row.on_hand
        for row in await session.scalars(
            select(StockAtLocation).where(StockAtLocation.sku == "ALN-ABAYA-01")
        )
    }
    assert shelves == {"online": 10, "jeddah": 0}


async def test_a_counter_sale_must_say_which_shop(client, shop):
    """The default that quietly emptied the wrong shelf.

    Every sale used to fall back to the storefront when the caller said nothing,
    which was wrong twice over for a shop: the shop that sold the units kept its
    number, and the storefront decrement was thrown away at the next Shopify pull
    because that shelf is overwritten with Shopify's own count. The stock left
    the building and no record moved.
    """
    res = await client.post("/sales", json={
        "receipt": "r-noshop",
        "lines": [{"sku": "ALN-ABAYA-01", "quantity": 1}],
    })
    assert res.status_code == 422
    assert "which shop" in res.json()["detail"]


async def test_an_online_order_needs_no_shop(client, session, shop):
    """The one caller with nothing to say. A website order came off the website's
    stock by definition, so asking it to name a shelf would be asking a question
    that has only one answer."""
    out = (await client.post("/sales", json={
        "receipt": "r-web",
        "source": "shopify",
        "channel": "online",
        "lines": [{"sku": "ALN-ABAYA-01", "quantity": 2}],
    })).json()

    assert out["accepted"]
    levels = list(await session.scalars(
        select(StockLevel)
        .where(StockLevel.sku == "ALN-ABAYA-01")
        .where(StockLevel.location_code == "online")
    ))
    assert [r.on_hand for r in levels] == [8]
