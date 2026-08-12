from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel, Field

from cdp.api.deps import ActorDep, SessionDep, SettingsDep
from cdp.connectors import shopify
from cdp.consent.service import ConsentService
from cdp.ingest.schemas import CanonicalEvent, IngestResult
from cdp.ingest.service import IngestService

router = APIRouter(prefix="/ingest", tags=["ingest"])


@router.post("/shopify", response_model=IngestResult)
async def shopify_webhook(
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    x_shopify_topic: Annotated[str | None, Header(alias="X-Shopify-Topic")] = None,
    x_shopify_hmac_sha256: Annotated[str | None, Header(alias="X-Shopify-Hmac-Sha256")] = None,
) -> IngestResult:
    # The raw bytes, not the parsed body: re-serialising changes the digest.
    body = await request.body()
    if not shopify.verify_webhook(settings.shopify_webhook_secret, body, x_shopify_hmac_sha256):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid Shopify HMAC")
    if not x_shopify_topic:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "missing X-Shopify-Topic")

    payload = await request.json()
    event = shopify.to_canonical(x_shopify_topic, payload)
    if event is None:
        # 200, not 4xx: an unhandled topic is our gap, and a non-2xx teaches
        # Shopify to retry it forever and eventually disable the subscription.
        return IngestResult(accepted=False)

    service = IngestService(session, actor="shopify", country_code=settings.default_country_code)
    return await service.ingest(event, topic=x_shopify_topic)


@router.post("/event", response_model=IngestResult)
async def generic_event(
    event: CanonicalEvent,
    session: SessionDep,
    settings: SettingsDep,
    actor: ActorDep,
) -> IngestResult:
    """Canonical-vocabulary intake for connectors that do not need bespoke
    translation — WhatsApp, the email platform, support. Keeps a new source a
    configuration exercise rather than a code change."""
    service = IngestService(session, actor=actor, country_code=settings.default_country_code)
    return await service.ingest(event)


class ActivationCapture(BaseModel):
    """The mall-activation form. This is the weakest link in the whole design: an
    offline cash buyer leaves no digital trace, so if nobody captures a phone
    number and a consent tick at the event, that touchpoint stays invisible
    forever. Hence phone is required and consent is explicit, not implied."""

    phone: str
    name: str | None = None
    email: str | None = None
    event_name: str = Field(default="mall_activation")
    # Which brand's stand she stood at. It is the brand she agreed with, so it
    # is required now rather than optional: a tick on a form with no brand on it
    # is not a grant anybody can rely on later.
    brand: str
    brand_interest: str | None = None
    language: str | None = None
    consent_marketing_whatsapp: bool = False
    consent_marketing_email: bool = False


@router.post("/activation-capture", response_model=IngestResult)
async def activation_capture(
    body: ActivationCapture,
    session: SessionDep,
    settings: SettingsDep,
    actor: ActorDep,
) -> IngestResult:
    now = datetime.now(UTC)
    event = CanonicalEvent(
        source="activation",
        name="lead_captured",
        # Two people at the same stand within the same second with the same phone
        # is a double submit, not two customers.
        dedupe_key=f"activation:{body.event_name}:{body.phone}:{now.isoformat(timespec='seconds')}",
        occurred_at=now,
        identifiers={"phone": body.phone, "email": body.email},
        channel="activation",
        language=body.language,
        display_name=body.name,
        payload={
            "event_name": body.event_name,
            "brand": body.brand,
            "brand_interest": body.brand_interest,
        },
    )
    service = IngestService(session, actor=actor, country_code=settings.default_country_code)
    result = await service.ingest(event)

    if result.person_id:
        consent = ConsentService(session, actor=actor)
        for purpose, granted in (
            ("marketing_whatsapp", body.consent_marketing_whatsapp),
            ("marketing_email", body.consent_marketing_email),
        ):
            await consent.record(
                result.person_id,
                purpose,
                granted,
                brand=body.brand,
                source="activation_form",
                evidence=f"{body.event_name} capture form",
                occurred_at=now,
            )
    return result
