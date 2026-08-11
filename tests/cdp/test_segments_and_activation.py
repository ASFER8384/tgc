from decimal import Decimal

import pytest
from sqlalchemy import select

from cdp.activation.port import MockDestination, WhatsAppFlowDestination
from cdp.activation.service import ActivationService
from cdp.models import ActivationDelivery, ProfileTraits, Segment
from cdp.segments.compiler import SegmentDefinitionError, compile_segment
from tests.cdp.conftest import SHOPIFY_SECRET
from tests.cdp.factories import shopify_order, signed

CROSS_SELL = {
    "all": [
        {"brand_purchased": "aleena"},
        {"brand_not_purchased": "rawash"},
        {"trait": "aov", "op": "gte", "value": 400},
    ]
}


async def ingest_order(client, **kwargs) -> str:
    payload = shopify_order(**kwargs)
    body, mac = signed(SHOPIFY_SECRET, payload)
    response = await client.post(
        "/ingest/shopify",
        content=body,
        headers={
            "X-Shopify-Topic": "orders/paid",
            "X-Shopify-Hmac-Sha256": mac,
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["person_id"]


async def aleena_only_buyer(client) -> str:
    return await ingest_order(
        client,
        order_id=6001,
        email="hessa@gmail.com",
        phone="0555000111",
        customer_id=9101,
        total="520.00",
        lines=[("Aleena", "520.00", 1)],
    )


async def grant(client, person_id: str, purpose: str, granted: bool = True) -> None:
    response = await client.post(
        f"/persons/{person_id}/consent",
        json={"purpose": purpose, "granted": granted, "source": "test"},
    )
    assert response.status_code == 200, response.text


async def create_segment(client, *, required_consent: str | None = "marketing_whatsapp") -> None:
    response = await client.post(
        "/segments",
        json={
            "key": "aleena_no_rawash",
            "name": "Aleena buyers who have never tried Rawash",
            "definition": CROSS_SELL,
            "required_consent": required_consent,
        },
    )
    assert response.status_code == 201, response.text


async def test_cross_sell_segment_selects_the_right_customer(client) -> None:
    target = await aleena_only_buyer(client)
    # Bought both brands already — not a cross-sell target.
    await ingest_order(
        client, order_id=6002, email="mona@gmail.com", phone="0555000222", customer_id=9102
    )
    # Aleena buyer below the AOV floor.
    await ingest_order(
        client,
        order_id=6003,
        email="lama@gmail.com",
        phone="0555000333",
        customer_id=9103,
        total="150.00",
        lines=[("Aleena", "150.00", 1)],
    )

    for person in (target,):
        await grant(client, person, "marketing_whatsapp")

    await create_segment(client)
    result = await client.post("/segments/aleena_no_rawash/evaluate")
    assert result.json()["person_ids"] == [target]


async def test_consent_is_enforced_by_the_query_not_by_discipline(client) -> None:
    target = await aleena_only_buyer(client)
    await create_segment(client)

    # No consent yet: the customer qualifies on behaviour and is still excluded.
    assert (await client.post("/segments/aleena_no_rawash/evaluate")).json()["size"] == 0

    await grant(client, target, "marketing_whatsapp")
    assert (await client.post("/segments/aleena_no_rawash/evaluate")).json()["size"] == 1

    # The demo kill-switch: revoke, and she falls out immediately.
    await grant(client, target, "marketing_whatsapp", granted=False)
    assert (await client.post("/segments/aleena_no_rawash/evaluate")).json()["size"] == 0


async def test_activation_delivers_and_logs_per_person(client, session) -> None:
    target = await aleena_only_buyer(client)
    await grant(client, target, "marketing_whatsapp")
    await create_segment(client)

    destination = MockDestination()
    service = ActivationService(session, registry={"mock": destination}, actor="test")
    run = await service.run("aleena_no_rawash", "mock")

    assert (run.requested, run.delivered, run.skipped_no_consent) == (1, 1, 0)
    assert [ctx.person_id for ctx in destination.delivered] == [target]
    assert destination.delivered[0].identifiers["phone"] == "+966555000111"
    assert destination.delivered[0].traits["ltv"] == "520.00"

    logged = list(await session.scalars(select(ActivationDelivery)))
    assert [(d.person_id, d.status, d.consent_basis) for d in logged] == [
        (target, "delivered", "marketing_whatsapp")
    ]


async def test_destination_reasserts_its_own_consent_purpose(client, session) -> None:
    """A segment gated on one purpose cannot smuggle people into a destination
    that needs another. The skip is counted, not hidden."""
    target = await aleena_only_buyer(client)
    await grant(client, target, "personalization")
    await create_segment(client, required_consent="personalization")

    destination = MockDestination()  # requires marketing_whatsapp
    run = await ActivationService(session, registry={"mock": destination}, actor="test").run(
        "aleena_no_rawash", "mock"
    )

    assert (run.requested, run.delivered, run.skipped_no_consent) == (1, 0, 1)
    assert destination.delivered == []


async def test_unconfigured_real_destination_fails_loudly(client, session) -> None:
    target = await aleena_only_buyer(client)
    await grant(client, target, "marketing_whatsapp")
    await create_segment(client)

    run = await ActivationService(
        session, registry={"whatsapp_flow": WhatsAppFlowDestination()}, actor="test"
    ).run("aleena_no_rawash", "whatsapp_flow")

    # A no-op that reported success is how a campaign "runs" for a week
    # without sending anything.
    assert (run.delivered, run.failed) == (0, 1)


async def test_merged_person_is_counted_once(client, session) -> None:
    online = await ingest_order(
        client, order_id=6101, email="reem@gmail.com", phone=None, customer_id=9201,
        total="520.00", lines=[("Aleena", "520.00", 1)],
    )
    instore = await ingest_order(
        client, order_id=6102, email=None, phone="0577000111", customer_id=None,
        total="600.00", lines=[("Aleena", "600.00", 1)],
        processed_at="2026-07-16T12:00:00+03:00", updated_at="2026-07-16T12:00:00+03:00",
        source_name="pos",
    )
    assert online != instore

    # A later order carrying both identities merges them.
    await ingest_order(
        client, order_id=6103, email="reem@gmail.com", phone="0577000111", customer_id=9201,
        total="480.00", lines=[("Aleena", "480.00", 1)],
        processed_at="2026-07-18T12:00:00+03:00", updated_at="2026-07-18T12:00:00+03:00",
    )

    survivor = (await client.get(f"/persons/{instore}")).json()["person_id"]
    await grant(client, survivor, "marketing_whatsapp")
    await create_segment(client)

    result = (await client.post("/segments/aleena_no_rawash/evaluate")).json()
    assert result["person_ids"] == [survivor]

    traits = await session.get(ProfileTraits, survivor)
    assert traits.order_count == 3
    assert traits.ltv == Decimal("1600.00")


def test_definition_errors_are_caught_at_authoring_time() -> None:
    with pytest.raises(SegmentDefinitionError):
        compile_segment({"all": [{"trait": "password", "op": "eq", "value": "x"}]}, None)
    with pytest.raises(SegmentDefinitionError):
        compile_segment({"all": [{"trait": "ltv", "op": "regex", "value": ".*"}]}, None)
    with pytest.raises(SegmentDefinitionError):
        compile_segment({"all": [{"trait": "ltv", "op": "gte", "value": "many"}]}, None)
    with pytest.raises(SegmentDefinitionError):
        compile_segment({"all": []}, "not_a_purpose")


async def test_bad_definition_is_rejected_by_the_api(client) -> None:
    response = await client.post(
        "/segments",
        json={"key": "bad", "name": "bad", "definition": {"all": [{"trait": "nope"}]}},
    )
    assert response.status_code == 422


async def test_stored_members_are_a_cache_not_the_truth(client, session) -> None:
    target = await aleena_only_buyer(client)
    await grant(client, target, "marketing_whatsapp")
    await create_segment(client)
    await client.post("/segments/aleena_no_rawash/evaluate")

    # Membership was materialised while she had consent. Revoke it and activate
    # without re-evaluating: activation must re-run the definition.
    await grant(client, target, "marketing_whatsapp", granted=False)
    destination = MockDestination()
    run = await ActivationService(session, registry={"mock": destination}, actor="test").run(
        "aleena_no_rawash", "mock"
    )
    assert run.requested == 0
    assert destination.delivered == []


async def test_segment_upsert_is_idempotent(client, session) -> None:
    await create_segment(client)
    await create_segment(client, required_consent="personalization")
    segments = list(await session.scalars(select(Segment)))
    assert len(segments) == 1
    assert segments[0].required_consent == "personalization"
