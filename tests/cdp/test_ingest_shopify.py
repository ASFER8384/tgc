from decimal import Decimal

from sqlalchemy import func, select

from cdp.connectors import shopify
from cdp.models import Event, Person, PersonBrandStat, ProfileTraits, RawEvent
from tests.cdp.conftest import SHOPIFY_SECRET
from tests.cdp.factories import shopify_customer, shopify_order, signed


def headers(topic: str, hmac_value: str) -> dict[str, str]:
    return {
        "X-Shopify-Topic": topic,
        "X-Shopify-Hmac-Sha256": hmac_value,
        "Content-Type": "application/json",
    }


async def post_order(client, payload: dict, topic: str = "orders/paid"):
    body, mac = signed(SHOPIFY_SECRET, payload)
    return await client.post("/ingest/shopify", content=body, headers=headers(topic, mac))


async def test_unsigned_webhook_is_rejected(client) -> None:
    response = await client.post(
        "/ingest/shopify",
        json=shopify_order(),
        headers={"X-Shopify-Topic": "orders/paid", "X-Shopify-Hmac-Sha256": "not-the-mac"},
    )
    assert response.status_code == 401


async def test_verification_runs_on_raw_bytes(client) -> None:
    payload = shopify_order()
    body, mac = signed(SHOPIFY_SECRET, payload)
    # Same object, different serialisation — the digest must not survive it,
    # which is what proves we are checking the bytes as received.
    reserialized = body.replace(b'{"id"', b'{ "id"')
    response = await client.post(
        "/ingest/shopify", content=reserialized, headers=headers("orders/paid", mac)
    )
    assert response.status_code == 401


async def test_paid_order_creates_profile_traits_and_brand_split(client, session) -> None:
    response = await post_order(client, shopify_order())
    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] and not body["duplicate"]

    traits = await session.get(ProfileTraits, body["person_id"])
    assert traits.order_count == 1
    assert traits.ltv == Decimal("640.00")
    assert traits.aov == Decimal("640.00")
    assert traits.brands_purchased == 2

    brands = {
        row.brand: row.spend
        for row in await session.scalars(
            select(PersonBrandStat).where(PersonBrandStat.person_id == body["person_id"])
        )
    }
    assert brands == {"aleena": Decimal("520.00"), "rawash": Decimal("120.00")}


async def test_redelivery_is_not_double_counted(client, session) -> None:
    payload = shopify_order()
    first = await post_order(client, payload)
    second = await post_order(client, payload)

    assert second.json()["duplicate"] is True
    assert await session.scalar(select(func.count()).select_from(RawEvent)) == 1
    assert await session.scalar(select(func.count()).select_from(Event)) == 1

    traits = await session.get(ProfileTraits, first.json()["person_id"])
    assert traits.ltv == Decimal("640.00"), "a doubled order destroys trust in every number"


async def test_a_genuine_edit_to_the_same_order_is_ingested(client, session) -> None:
    await post_order(client, shopify_order())
    edited = shopify_order(total="700.00", updated_at="2026-07-14T12:00:00+03:00")
    response = await post_order(client, edited)
    assert response.json()["duplicate"] is False
    assert await session.scalar(select(func.count()).select_from(Event)) == 2


async def test_online_and_pos_orders_land_on_one_person(client, session) -> None:
    online = await post_order(client, shopify_order(order_id=5001))
    pos = await post_order(
        client,
        shopify_order(
            order_id=5002,
            source_name="pos",
            total="180.00",
            lines=[("Rawash", "180.00", 1)],
            processed_at="2026-07-20T18:05:00+03:00",
            updated_at="2026-07-20T18:05:00+03:00",
        ),
    )
    assert pos.json()["person_id"] == online.json()["person_id"]
    assert await session.scalar(select(func.count()).select_from(Person)) == 1

    traits = await session.get(ProfileTraits, online.json()["person_id"])
    assert traits.order_count == 2
    assert traits.ltv == Decimal("820.00")

    channels = {
        row.channel
        for row in await session.scalars(
            select(Event).where(Event.person_id == online.json()["person_id"])
        )
    }
    assert channels == {"online", "retail"}


async def test_unpaid_order_is_timeline_context_not_revenue(client, session) -> None:
    response = await post_order(
        client, shopify_order(financial_status="pending"), topic="orders/create"
    )
    person_id = response.json()["person_id"]
    traits = await session.get(ProfileTraits, person_id)
    assert traits.order_count == 0
    assert traits.ltv == Decimal("0")

    event = await session.scalar(select(Event).where(Event.person_id == person_id))
    assert event.name == "checkout_started"


async def test_customer_topic_indexes_the_customer_id_not_the_order_id(client, session) -> None:
    body, mac = signed(SHOPIFY_SECRET, shopify_customer(customer_id=9002))
    created = await client.post(
        "/ingest/shopify", content=body, headers=headers("customers/create", mac)
    )
    person_id = created.json()["person_id"]

    # A later order for the same Shopify customer must attach, not fork.
    order = await post_order(
        client, shopify_order(customer_id=9002, email="sara.otaibi@outlook.com", phone="0501230000")
    )
    assert order.json()["person_id"] == person_id


async def test_unmapped_vendor_is_visible_but_not_a_fourth_brand(client, session) -> None:
    response = await post_order(
        client,
        shopify_order(total="90.00", lines=[("SomeNewVendor", "90.00", 1)]),
    )
    person_id = response.json()["person_id"]
    traits = await session.get(ProfileTraits, person_id)
    assert traits.order_count == 1
    assert traits.ltv == Decimal("90.00")
    assert traits.brands_purchased == 0

    event = await session.scalar(select(Event).where(Event.person_id == person_id))
    assert event.payload["brands"] == {"unassigned": "90.00"}


async def test_unsupported_topic_is_acknowledged_not_retried_forever(client) -> None:
    body, mac = signed(SHOPIFY_SECRET, {"id": 1})
    response = await client.post(
        "/ingest/shopify", content=body, headers=headers("inventory_levels/update", mac)
    )
    assert response.status_code == 200
    assert response.json()["accepted"] is False


def test_line_discounts_reduce_brand_spend() -> None:
    payload = shopify_order(total="470.00")
    payload["line_items"][0]["discount_allocations"] = [{"amount": "50.00"}]
    event = shopify.to_canonical("orders/paid", payload)
    assert event.brands["aleena"] == Decimal("470.00")
    assert event.brands["rawash"] == Decimal("120.00")


def test_a_cart_token_is_not_recorded_as_a_device() -> None:
    """One checkout, one token, never seen again — so a customer with forty
    orders was acquiring forty "devices" and burying the identifiers a human
    recognises her by."""
    event = shopify.to_canonical("orders/paid", shopify_order(order_id=7801))

    assert event.identifiers["cart_token"] == "cart-token-7801"
    # No storefront pixel in the payload, so there is genuinely no device here.
    assert event.identifiers["device_id"] is None


def test_the_pixel_id_is_still_a_device() -> None:
    payload = shopify_order(order_id=7802)
    payload["client_details"] = {"browser_ip_hash": "abc123"}

    event = shopify.to_canonical("orders/paid", payload)

    assert event.identifiers["device_id"] == "abc123"
    assert event.identifiers["cart_token"] == "cart-token-7802"
