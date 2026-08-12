from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select

from cdp.models import AuditLog, ConsentEvent, ProfileTraits
from cdp.profiles.service import ProfileService
from tests.cdp.conftest import SHOPIFY_SECRET
from tests.cdp.factories import shopify_order, signed


async def ingest(client, payload: dict, topic: str = "orders/paid") -> dict:
    body, mac = signed(SHOPIFY_SECRET, payload)
    response = await client.post(
        "/ingest/shopify",
        content=body,
        headers={
            "X-Shopify-Topic": topic,
            "X-Shopify-Hmac-Sha256": mac,
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_one_customer_one_timeline_across_three_channels(client) -> None:
    """The demo moment: online + retail + WhatsApp on a single profile."""
    online = await ingest(client, shopify_order(order_id=7001))
    person_id = online["person_id"]

    await ingest(
        client,
        shopify_order(
            order_id=7002,
            source_name="pos",
            total="180.00",
            lines=[("Rawash", "180.00", 1)],
            processed_at="2026-07-20T18:05:00+03:00",
            updated_at="2026-07-20T18:05:00+03:00",
        ),
    )

    whatsapp = await client.post(
        "/ingest/event",
        json={
            "source": "whatsapp",
            "name": "message_in",
            "dedupe_key": "wa:msg:abc123",
            "occurred_at": "2026-07-21T09:15:00+03:00",
            "identifiers": {"phone": "0501234567", "whatsapp_id": "966501234567"},
            "channel": "whatsapp",
            "payload": {"text": "هل يوجد مقاس M؟"},
        },
    )
    assert whatsapp.json()["person_id"] == person_id

    profile = (await client.get(f"/persons/{person_id}")).json()
    assert {e["source"] for e in profile["timeline"]} == {"shopify", "shopify_pos", "whatsapp"}
    assert {b["brand"] for b in profile["brands"]} == {"aleena", "rawash"}
    assert profile["traits"]["order_count"] == 2
    assert Decimal(profile["traits"]["ltv"]) == Decimal("820.00")
    assert profile["preferred_language"] == "ar"


async def test_profile_read_is_audited(client, session) -> None:
    person_id = (await ingest(client, shopify_order(order_id=7010)))["person_id"]
    await client.get(f"/persons/{person_id}")
    reads = list(
        await session.scalars(select(AuditLog).where(AuditLog.action == "person.read"))
    )
    assert [r.entity_id for r in reads] == [person_id]


async def test_old_id_still_resolves_after_a_merge(client) -> None:
    first = (await ingest(client, shopify_order(order_id=7020, phone=None, customer_id=None)))[
        "person_id"
    ]
    second = (
        await ingest(
            client,
            shopify_order(
                order_id=7021,
                email=None,
                phone="0533111222",
                customer_id=None,
                processed_at="2026-07-15T10:00:00+03:00",
                updated_at="2026-07-15T10:00:00+03:00",
            ),
        )
    )["person_id"]
    merged = (
        await ingest(
            client,
            shopify_order(
                order_id=7022,
                phone="0533111222",
                customer_id=None,
                processed_at="2026-07-16T10:00:00+03:00",
                updated_at="2026-07-16T10:00:00+03:00",
            ),
        )
    )["person_id"]

    assert {first, second} >= {merged} or merged in {first, second}
    for old in (first, second):
        response = await client.get(f"/persons/{old}")
        assert response.status_code == 200
        assert response.json()["person_id"] == merged


async def test_activation_capture_records_consent_with_evidence(client, session) -> None:
    response = await client.post(
        "/ingest/activation-capture",
        json={
            "phone": "0561234567",
            "name": "Latifa",
            "event_name": "riyadh_park_popup",
            "brand": "rawash",
            "brand_interest": "rawash",
            "language": "ar",
            "consent_marketing_whatsapp": True,
            "consent_marketing_email": False,
        },
    )
    assert response.status_code == 200
    person_id = response.json()["person_id"]

    rows = {
        row.purpose: row
        for row in await session.scalars(
            select(ConsentEvent).where(ConsentEvent.person_id == person_id)
        )
    }
    assert rows["marketing_whatsapp"].granted is True
    assert rows["marketing_email"].granted is False
    # "Prove she agreed" is the question a regulator asks — and the answer has
    # to name the brand she agreed with, not the company she never met.
    assert rows["marketing_whatsapp"].source == "activation_form"
    assert rows["marketing_whatsapp"].brand == "rawash"
    assert "riyadh_park_popup" in rows["marketing_whatsapp"].evidence

    profile = (await client.get(f"/persons/{person_id}")).json()
    assert profile["identifiers"]["phone"] == ["+966561234567"]
    assert profile["consent"]["rawash"]["marketing_whatsapp"] is True
    assert profile["consent"]["rawash"]["personalization"] is False
    # She stood at the Rawash stand. Aleena was not part of that conversation.
    assert profile["consent"]["aleena"]["marketing_whatsapp"] is False


async def test_unknown_consent_purpose_is_rejected(client) -> None:
    person_id = (await ingest(client, shopify_order(order_id=7030)))["person_id"]
    response = await client.post(
        f"/persons/{person_id}/consent",
        json={"purpose": "sell_to_anyone", "granted": True, "brand": "aleena"},
    )
    assert response.status_code == 400


async def test_protected_routes_require_a_key(client) -> None:
    response = await client.get("/persons/whatever", headers={"X-API-Key": "wrong"})
    assert response.status_code == 401


async def test_refund_reverses_ltv(client, session) -> None:
    order = await ingest(client, shopify_order(order_id=7040))
    person_id = order["person_id"]

    await client.post(
        "/ingest/event",
        json={
            "source": "shopify",
            "name": "order_refunded",
            "dedupe_key": "shopify:refund:7040",
            "occurred_at": "2026-07-15T10:00:00+03:00",
            "identifiers": {"shopify_customer_id": "9001"},
            "payload": {"refunded_order_id": 7040},
        },
    )
    # The refund names the order it reverses, so recompute drops it from LTV.
    traits = await session.get(ProfileTraits, person_id)
    await session.refresh(traits)
    assert traits.order_count == 0
    assert traits.ltv == Decimal("0")


async def test_rfm_scores_a_recent_frequent_high_value_customer_highly(session) -> None:
    from cdp.models import Event, Person

    person = Person()
    session.add(person)
    await session.flush()
    for i in range(9):
        session.add(
            Event(
                person_id=person.id,
                source="shopify",
                name="order_paid",
                occurred_at=datetime(2026, 8, 1, tzinfo=UTC),
                value_amount=Decimal("600.00"),
                currency="SAR",
                channel="online",
                payload={"brands": {"aleena": "600.00"}, "order_id": i},
            )
        )
    await session.flush()

    service = ProfileService(session, now=datetime(2026, 8, 2, tzinfo=UTC))
    traits = await service.recompute(person.id)
    assert traits.rfm == "555"


async def test_preferred_channel_reflects_where_she_actually_engages(client) -> None:
    person_id = (await ingest(client, shopify_order(order_id=7050)))["person_id"]
    for i in range(3):
        await client.post(
            "/ingest/event",
            json={
                "source": "whatsapp",
                "name": "message_in",
                "dedupe_key": f"wa:seed:{i}",
                "occurred_at": "2026-07-21T09:15:00+03:00",
                "identifiers": {"shopify_customer_id": "9001"},
                "channel": "whatsapp",
                "payload": {},
            },
        )
    # Campaign design leans on this field hardest in a WhatsApp-first market, so
    # it has to be written, not merely calculated.
    profile = (await client.get(f"/persons/{person_id}")).json()
    assert profile["preferred_channel"] == "whatsapp"
