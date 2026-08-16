"""The inbound half of WhatsApp: a public URL anybody can post to.

Every test here is about that fact. The email side is pulled out of a mailbox
this system already has credentials for; this one is pushed to an address on the
open internet, so what it refuses matters more than what it accepts.
"""

import hashlib
import hmac
import json

import pytest

from sca.config import Settings

pytestmark = pytest.mark.asyncio

SECRET = "app-secret-for-tests"
VERIFY = "verify-token-for-tests"


def _signed(body: dict) -> tuple[bytes, str]:
    raw = json.dumps(body).encode()
    digest = hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return raw, f"sha256={digest}"


def _reply(text: str, sender: str, *, message_id: str = "wamid.TEST1") -> dict:
    return {
        "entry": [{"changes": [{"value": {"messages": [{
            "id": message_id, "from": sender, "timestamp": "1786900000",
            "type": "text", "text": {"body": text},
        }]}}]}]
    }


@pytest.fixture
def wired(client):
    """The webhook's two secrets, present. Without them it refuses everything,
    which is the correct default and useless for testing what it accepts."""
    from sca.api.deps import runtime_settings

    def _settings() -> Settings:
        return Settings(_env_file=None, wa_app_secret=SECRET, wa_verify_token=VERIFY)

    client.app.dependency_overrides[runtime_settings] = _settings
    yield
    client.app.dependency_overrides.pop(runtime_settings, None)


async def _inbound(sessionmaker_fixture):
    from sqlalchemy import select

    from sca.models import InboundMessage

    async with sessionmaker_fixture() as session:
        return list(await session.scalars(select(InboundMessage)))


async def test_the_handshake_needs_the_right_token(client, wired):
    ok = await client.get("/webhooks/whatsapp", params={
        "hub.mode": "subscribe", "hub.verify_token": VERIFY, "hub.challenge": "42",
    })
    assert ok.status_code == 200
    assert ok.text == "42"

    wrong = await client.get("/webhooks/whatsapp", params={
        "hub.mode": "subscribe", "hub.verify_token": "guess", "hub.challenge": "42",
    })
    assert wrong.status_code == 403


async def test_an_unsigned_body_is_refused(client, wired):
    """The one that matters. Without the signature check this endpoint lets a
    stranger acknowledge an order, move it out of chasing, and stop anybody here
    noticing a mill had gone quiet."""
    raw, _ = _signed(_reply("confirmed", "918220958384"))
    refused = await client.post("/webhooks/whatsapp", content=raw)
    assert refused.status_code == 403

    bad = await client.post(
        "/webhooks/whatsapp", content=raw,
        headers={"X-Hub-Signature-256": "sha256=" + "0" * 64},
    )
    assert bad.status_code == 403


async def test_a_reply_is_filed_against_the_supplier_who_sent_it(
    client, supplier_payload, wired, sessionmaker_fixture
):
    supplier = (await client.post("/suppliers", json=supplier_payload | {
        "phone": "+91 82209 58384", "country": "IN",
    })).json()

    raw, signature = _signed(_reply("confirmed, shipping Tuesday", "918220958384"))
    out = await client.post(
        "/webhooks/whatsapp", content=raw,
        headers={"X-Hub-Signature-256": signature},
    )
    assert out.status_code == 200
    assert out.json()["messages"] == 1

    rows = await _inbound(sessionmaker_fixture)
    mine = [m for m in rows if m.from_address == "918220958384"]
    assert mine, "the reply is in the record"
    assert mine[0].supplier_id == supplier["id"], "matched on digits, not on text"
    assert mine[0].source == "whatsapp"


async def test_the_same_push_twice_is_not_two_replies(
    client, supplier_payload, wired, sessionmaker_fixture
):
    """Meta retries anything that is not a 200, and retries a 200 it did not
    hear. One confirmation applied twice is an order acknowledged twice."""
    await client.post("/suppliers", json=supplier_payload | {
        "phone": "+91 82209 58384", "country": "IN",
    })
    raw, signature = _signed(_reply("confirmed", "918220958384", message_id="wamid.SAME"))
    first = await client.post("/webhooks/whatsapp", content=raw,
                              headers={"X-Hub-Signature-256": signature})
    second = await client.post("/webhooks/whatsapp", content=raw,
                               headers={"X-Hub-Signature-256": signature})
    assert first.status_code == second.status_code == 200

    rows = await _inbound(sessionmaker_fixture)
    same = [m for m in rows if m.external_id == "wamid.SAME"]
    assert len(same) == 1


async def test_a_delivery_receipt_is_not_a_reply(client, wired):
    """Status callbacks arrive on the same webhook. Filing them would fill the
    conversation with our own messages coming back at us."""
    raw, signature = _signed({"entry": [{"changes": [{"value": {
        "statuses": [{"id": "wamid.X", "status": "delivered"}]
    }}]}]})
    out = await client.post("/webhooks/whatsapp", content=raw,
                            headers={"X-Hub-Signature-256": signature})
    assert out.status_code == 200
    assert out.json()["messages"] == 0
