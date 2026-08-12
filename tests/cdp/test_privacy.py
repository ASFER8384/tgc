"""Erasure and subject access.

The obligation is easy to satisfy badly: delete the row you were handed, return
200, and leave her name in the raw webhook bodies and her history under a merged
alias. These tests exist to make that failure impossible rather than unlikely.
"""

from sqlalchemy import func, select

from cdp.models import (
    AuditLog,
    ConsentEvent,
    Event,
    Identifier,
    Person,
    ProfileTraits,
    RawEvent,
)
from cdp.privacy.service import PrivacyService
from tests.cdp.conftest import SHOPIFY_SECRET
from tests.cdp.factories import shopify_order, signed


async def ingest(client, payload: dict) -> dict:
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
    return response.json()


async def _count(session, model) -> int:
    return await session.scalar(select(func.count()).select_from(model))


async def test_export_returns_what_is_actually_held(client) -> None:
    order = await ingest(client, shopify_order(order_id=7900, email="noura@gmail.com"))
    person_id = order["person_id"]
    await client.post(
        f"/persons/{person_id}/consent",
        json={"purpose": "marketing_whatsapp", "granted": True, "brand": "aleena",
              "source": "shopify_checkout"},
    )

    dump = (await client.get(f"/persons/{person_id}/export")).json()

    assert person_id in dump["person_ids"]
    assert {i["kind"] for i in dump["identifiers"]} >= {"email", "phone"}
    assert len(dump["events"]) == 1
    # The ledger, not the current state: "prove she agreed on 3 March".
    assert dump["consent"][0]["brand"] == "aleena"
    assert dump["consent"][0]["granted"] is True
    assert dump["traits"][0]["order_count"] == 1


async def test_erasure_removes_the_raw_payloads_too(client, session) -> None:
    """The webhook bodies carry her name, address and telephone number. An
    erasure that tidied the normalised tables and left those behind would be a
    deletion in name only — and the raw store is where anyone checking would
    look first."""
    order = await ingest(client, shopify_order(order_id=7901, email="hessa@gmail.com"))
    assert await _count(session, RawEvent) == 1

    response = await client.delete(f"/persons/{order['person_id']}")

    assert response.status_code == 200
    assert response.json()["deleted"]["raw_events"] == 1
    for model in (Person, Identifier, Event, RawEvent, ProfileTraits, ConsentEvent):
        assert await _count(session, model) == 0, model.__name__


async def test_erasure_follows_a_merge(client, session) -> None:
    """She bought online, then in store under a second Shopify customer id. The
    two rows were merged on her phone. Deleting the id the caller happens to
    hold must not leave her alive under the other one."""
    first = await ingest(
        client,
        shopify_order(order_id=7902, email="layla@gmail.com", phone=None, customer_id=9401),
    )
    second = await ingest(
        client,
        shopify_order(order_id=7903, email=None, phone="0533221100", customer_id=9402),
    )
    # Same woman, evidenced by an order carrying both identifiers at once.
    merged = await ingest(
        client,
        shopify_order(
            order_id=7904, email="layla@gmail.com", phone="0533221100", customer_id=9401
        ),
    )
    assert first["person_id"] != second["person_id"]

    cluster = await PrivacyService(session).cluster(merged["person_id"])
    assert {first["person_id"], second["person_id"]} <= set(cluster)

    # Delete using the id that lost the merge — the stale one a downstream
    # system would still be holding.
    response = await client.delete(f"/persons/{second['person_id']}")

    assert response.status_code == 200
    assert await _count(session, Person) == 0
    assert await _count(session, Identifier) == 0
    assert await _count(session, Event) == 0


async def test_the_erasure_is_recorded_without_recording_her(client, session) -> None:
    """Proof that a deletion happened has to survive the deletion. The record
    holds counts and ids, and nothing that identifies her."""
    order = await ingest(client, shopify_order(order_id=7905, email="mona@gmail.com"))

    await client.delete(f"/persons/{order['person_id']}?reason=customer%20email")

    entry = await session.scalar(
        select(AuditLog).where(AuditLog.action == "person.erased")
    )
    assert entry is not None
    assert entry.meta["reason"] == "customer email"
    assert entry.meta["deleted"]["persons"] == 1
    serialised = str(entry.meta)
    assert "mona@gmail.com" not in serialised


async def test_erasing_an_unknown_person_is_a_404(client) -> None:
    assert (await client.delete("/persons/nobody")).status_code == 404
    assert (await client.get("/persons/nobody/export")).status_code == 404
