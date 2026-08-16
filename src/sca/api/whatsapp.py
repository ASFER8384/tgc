"""Supplier replies arriving from WhatsApp.

The other half of the loop, and a different shape from the email one. IMAP is
pulled when this system decides to look; Meta pushes, to a public URL, whenever
it likes. That difference is the whole reason this file exists:

- a public URL is one anybody can post to, so every delivery is checked against
  Meta's signature before a word of it is believed;
- a push can arrive twice, so ingestion is keyed on Meta's message id and a
  repeat is recognised rather than applied again;
- a push has no subject line, so the order a reply belongs to is worked out from
  the number it came from and the last order sent to them.

Meta retries anything that is not a 200 for up to several days, so this answers
200 to a body it cannot use. A reply it could not parse is a row in the record
and an issue for a person, not a redelivery every hour.
"""

import hashlib
import hmac
import json
from datetime import UTC, datetime

from fastapi import APIRouter, Header, HTTPException, Request, Response, status

from sca.api.deps import ActorDep, RuntimeSettingsDep, SessionDep
from sca.inbound.service import InboundService

router = APIRouter(tags=["whatsapp"])


@router.get("/webhooks/whatsapp")
async def verify(
    request: Request, settings: RuntimeSettingsDep
) -> Response:
    """Meta's one-time handshake when the webhook is registered.

    Answers with the challenge only when the token matches. The comparison is
    constant time for the same reason the signature check below is: a token that
    can be discovered a character at a time is not a token.
    """
    params = request.query_params
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token") or ""
    challenge = params.get("hub.challenge") or ""
    expected = settings.wa_verify_token or ""
    if mode == "subscribe" and expected and hmac.compare_digest(token, expected):
        return Response(content=challenge, media_type="text/plain")
    raise HTTPException(status.HTTP_403_FORBIDDEN, "verify token does not match")


def _signed_by_meta(body: bytes, header: str | None, secret: str | None) -> bool:
    """Whether this body really came from Meta.

    Without this the endpoint is a public URL that accepts supplier
    confirmations — anyone who finds it could acknowledge an order, move it out
    of chasing, and stop anybody here noticing a mill had gone quiet.
    """
    if not secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(header.removeprefix("sha256="), expected)


@router.post("/webhooks/whatsapp")
async def receive(
    request: Request,
    session: SessionDep,
    actor: ActorDep,
    settings: RuntimeSettingsDep,
    x_hub_signature_256: str | None = Header(default=None),
) -> dict:
    raw = await request.body()
    if not _signed_by_meta(raw, x_hub_signature_256, settings.wa_app_secret):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "signature does not match")

    try:
        payload = json.loads(raw or b"{}")
    except ValueError:
        # A 200, deliberately. Meta retries anything else for days, and a body
        # this cannot read will not become readable on the fourth attempt.
        return {"accepted": False, "reason": "body is not json"}

    service = InboundService(session, actor="whatsapp-agent")
    taken: list[dict] = []
    ignored = 0

    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            # Delivery receipts and read marks arrive on the same webhook as
            # messages. They are not replies and there is nothing to file.
            for message in value.get("messages", []):
                text = (message.get("text") or {}).get("body")
                if not text:
                    # A photograph of a packing list is worth having and is not
                    # this change: media arrives as an id that has to be fetched
                    # with a second call. Counted rather than dropped silently.
                    ignored += 1
                    continue
                sender = message.get("from")
                supplier = await service.match_supplier_by_phone(sender)
                stamp = message.get("timestamp")
                received_at = (
                    datetime.fromtimestamp(int(stamp), tz=UTC)
                    if stamp else datetime.now(UTC)
                )
                result = await service.ingest(
                    external_id=message.get("id") or f"wa-{stamp}-{sender}",
                    from_address=sender,
                    # WhatsApp has no subject. Passing the supplier's name would
                    # invent one and feed the parser a line they never wrote.
                    subject=None,
                    body=text,
                    received_at=received_at,
                    source="whatsapp",
                    supplier=supplier,
                )
                taken.append(result)

    return {"accepted": True, "messages": len(taken), "ignored": ignored}
