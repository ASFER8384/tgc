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


async def test_meta_does_not_need_an_api_key(client, wired):
    """Found before the first real delivery: the webhook carried the same API key
    dependency as every other route, so Meta's push answered 401 and Meta would
    have retried it for days. Its authentication is the signature on the body —
    putting this service's API key in a third party's webhook console would be a
    shared secret that opens every other route here."""
    raw, signature = _signed(_reply("confirmed", "918220958384", message_id="wamid.NOKEY"))
    client.headers.pop("X-API-Key", None)
    out = await client.post("/webhooks/whatsapp", content=raw,
                            headers={"X-Hub-Signature-256": signature})
    assert out.status_code == 200, "no API key, and it still accepts a signed body"

    # And without a signature it is still refused, key or no key.
    refused = await client.post("/webhooks/whatsapp", content=raw)
    assert refused.status_code == 403


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


def _document(sender: str, *, message_id: str = "wamid.DOC") -> dict:
    return {
        "entry": [{"changes": [{"value": {"messages": [{
            "id": message_id, "from": sender, "timestamp": "1786900000",
            "type": "document", "document": {
                "id": "media-1", "filename": "invoice-4471.pdf",
                "mime_type": "application/pdf", "caption": "invoice for PO-1001",
            },
        }]}}]}]
    }


async def test_an_invoice_sent_as_a_pdf_is_downloaded_and_kept(
    client, supplier_payload, wired, sessionmaker_fixture, monkeypatch
):
    """Media arrives as an id, not as bytes, and the URL it resolves to expires
    within minutes. Before this the whole message was counted as ignored: a
    supplier who answered with the invoice attached had, as far as the record
    went, not answered at all."""
    from sqlalchemy import select

    from sca.models import Attachment
    from sca.whatsapp.base import Media

    async def _fake(media_id, settings, *, filename=None):
        return Media(media_id=media_id, filename=filename or "x",
                     content_type="application/pdf", content=b"%PDF-1.4 invoice")

    monkeypatch.setattr("sca.api.whatsapp.download_media", _fake)
    await client.post("/suppliers", json=supplier_payload | {
        "phone": "+91 82209 58384", "country": "IN",
    })

    raw, signature = _signed(_document("918220958384"))
    out = await client.post("/webhooks/whatsapp", content=raw,
                            headers={"X-Hub-Signature-256": signature})
    assert out.status_code == 200
    assert out.json() == {"accepted": True, "messages": 1, "files": 1, "ignored": 0}

    async with sessionmaker_fixture() as session:
        stored = list(await session.scalars(select(Attachment)))
    assert len(stored) == 1
    assert stored[0].filename == "invoice-4471.pdf", "their name for it, not ours"
    assert stored[0].content == b"%PDF-1.4 invoice"
    assert stored[0].sent_message_id is None, "it came in"

    rows = await _inbound(sessionmaker_fixture)
    assert "invoice for PO-1001" in rows[-1].body, "the caption is what they wrote"


async def test_a_file_that_will_not_download_still_leaves_the_message(
    client, supplier_payload, wired, sessionmaker_fixture, monkeypatch
):
    """Meta will not deliver this push again. Dropping the message because its
    attachment could not be fetched would lose the fact that the supplier sent an
    invoice at all — which is the part somebody has to chase."""
    from sca.whatsapp import WhatsAppError

    async def _fails(media_id, settings, *, filename=None):
        raise WhatsAppError("media 402 expired")

    monkeypatch.setattr("sca.api.whatsapp.download_media", _fails)
    await client.post("/suppliers", json=supplier_payload | {
        "phone": "+91 82209 58384", "country": "IN",
    })

    raw, signature = _signed(_document("918220958384", message_id="wamid.LOST"))
    out = await client.post("/webhooks/whatsapp", content=raw,
                            headers={"X-Hub-Signature-256": signature})
    assert out.status_code == 200
    assert out.json()["messages"] == 1
    assert out.json()["files"] == 0

    rows = await _inbound(sessionmaker_fixture)
    body = rows[-1].body
    assert "invoice for PO-1001" in body, "their words survive"
    assert "could not be downloaded" in body, "and the gap is stated, not hidden"


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
