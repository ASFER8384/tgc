from dataclasses import dataclass, field
from typing import Protocol

from cdp.models import PURPOSES


class NotConfigured(RuntimeError):
    """Raised when a real destination is selected without credentials. Explicit,
    because the alternative — a no-op that reports success — is how a campaign
    "runs" for a week without sending anything."""


@dataclass
class DeliveryContext:
    person_id: str
    segment_key: str
    identifiers: dict[str, str]
    traits: dict[str, object]
    consent_basis: str | None


@dataclass
class DeliveryResult:
    status: str  # delivered | failed | skipped
    detail: str | None = None


class Destination(Protocol):
    """Activation destinations are ports, mirroring the connector side.

    The consent purpose a destination *requires* is declared by the destination
    itself, not chosen per campaign: uploading a hashed phone to Meta needs
    ad_audience_sharing whatever the marketer intended, so it cannot be lowered
    by whoever is configuring the send.

    ``addressed`` says whether a message arrives at a named person carrying
    content derived from their history. A wrong identifier there delivers one
    customer's purchases to another, which is a disclosure and is not repaired
    by a correction afterwards — so those destinations refuse identifiers that
    may belong to a third party, whatever the consent state says.
    """

    name: str
    required_consent: str | None
    addressed: bool

    async def deliver(self, ctx: DeliveryContext) -> DeliveryResult: ...


@dataclass
class MockDestination:
    """Records deliveries in memory. This is what the demo and the whole test
    suite run against — a real send has to be a deliberate configuration step."""

    name: str = "mock"
    required_consent: str | None = "marketing_whatsapp"
    addressed: bool = True
    delivered: list[DeliveryContext] = field(default_factory=list)

    async def deliver(self, ctx: DeliveryContext) -> DeliveryResult:
        self.delivered.append(ctx)
        return DeliveryResult(status="delivered", detail="recorded by mock destination")


@dataclass
class WhatsAppFlowDestination:
    """Sends a template message via the WhatsApp Cloud API.

    Deliberately unimplemented in the base build: it needs an approved sender, an
    approved template per language, and a 24-hour-window policy decision that is
    the client's to make. The seam is here so wiring it is a contained change.
    """

    name: str = "whatsapp_flow"
    required_consent: str | None = "marketing_whatsapp"
    # A named person receives a message about what she bought.
    addressed: bool = True
    access_token: str | None = None
    phone_number_id: str | None = None
    template: str | None = None

    async def deliver(self, ctx: DeliveryContext) -> DeliveryResult:
        if not (self.access_token and self.phone_number_id and self.template):
            raise NotConfigured("whatsapp_flow needs access_token, phone_number_id and template")
        raise NotConfigured("whatsapp_flow send is not implemented in the base build")


@dataclass
class ShopifySegmentDestination:
    """Writes membership back to Shopify as a customer tag / metafield, which is
    what makes the storefront personalise. Same reasoning as above."""

    name: str = "shopify_segment"
    required_consent: str | None = "personalization"
    # A tag on her own store record — nothing is sent anywhere.
    addressed: bool = False
    shop: str | None = None
    access_token: str | None = None

    async def deliver(self, ctx: DeliveryContext) -> DeliveryResult:
        if not (self.shop and self.access_token):
            raise NotConfigured("shopify_segment needs shop and access_token")
        raise NotConfigured("shopify_segment write is not implemented in the base build")


def default_registry() -> dict[str, Destination]:
    return {
        "mock": MockDestination(),
        "whatsapp_flow": WhatsAppFlowDestination(),
        "shopify_segment": ShopifySegmentDestination(),
    }


def validate_destination(destination: Destination) -> None:
    if destination.required_consent is not None and destination.required_consent not in PURPOSES:
        raise ValueError(f"destination {destination.name} declares unknown consent purpose")
