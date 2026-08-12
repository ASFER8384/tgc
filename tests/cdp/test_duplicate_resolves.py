"""A redelivered webhook still names the customer it belongs to.

Found by a seed script: every order deduplicated on a second run, the duplicate
response carried no person, and so the consent the script meant to record was
silently skipped. Nothing errored — the data was simply never written.

"Already seen" and "could not be resolved" are different answers, and returning
the second for the first makes retries lossy for every caller that acts on the
person afterwards.
"""

from tests.cdp.conftest import SHOPIFY_SECRET
from tests.cdp.factories import shopify_order, signed


async def _ingest(client, payload) -> dict:
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


async def test_redelivery_returns_the_same_person(client):
    payload = shopify_order(order_id=7700, email="noura@gmail.com", phone="0555000999")

    first = await _ingest(client, payload)
    second = await _ingest(client, payload)

    assert first["duplicate"] is False
    assert second["duplicate"] is True
    # The point: a retry can still act on her.
    assert second["person_id"] == first["person_id"]


async def test_consent_can_be_recorded_after_a_redelivery(client):
    """The failure as it actually happened, end to end."""
    payload = shopify_order(order_id=7701, email="hessa@gmail.com", phone="0555000888")
    await _ingest(client, payload)
    repeat = await _ingest(client, payload)

    response = await client.post(
        f"/persons/{repeat['person_id']}/consent",
        json={"purpose": "marketing_whatsapp", "granted": True, "brand": "aleena",
              "source": "shopify_checkout"},
    )

    assert response.status_code == 200
    assert response.json()["aleena"]["marketing_whatsapp"] is True
