from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field

# Sources and event names are a closed vocabulary. Connectors translate into it;
# nothing downstream ever branches on a source-specific string, which is what
# keeps adding the seventh connector as cheap as the second.
SOURCES = ("shopify", "shopify_pos", "whatsapp", "email", "activation", "ads", "support")

PURCHASE_EVENTS = ("order_paid",)
EVENT_NAMES = (
    "order_paid",
    "order_refunded",
    "checkout_started",
    "product_viewed",
    "page_viewed",
    "message_in",
    "message_out",
    "email_opened",
    "email_clicked",
    "lead_captured",
    "ticket_opened",
)


class CanonicalEvent(BaseModel):
    """The contract between connectors and the core.

    Two fields carry more weight than the rest. `dedupe_key` must be derived from
    something stable in the source payload — it is the only thing standing between
    at-least-once webhook delivery and double-counted revenue. `occurred_at` is
    event time, so a two-year backfill can run beside live webhooks without the
    import overwriting fresher state.
    """

    source: str
    name: str
    dedupe_key: str
    occurred_at: datetime
    # kind -> raw value. Normalisation happens in the identity layer, never here,
    # so a connector cannot invent its own idea of what a phone number is.
    identifiers: dict[str, str | None] = Field(default_factory=dict)
    value_amount: Decimal | None = None
    currency: str | None = None
    channel: str | None = None
    # Per-brand split of value_amount: one basket can span Aleena and Rawash.
    brands: dict[str, Decimal] = Field(default_factory=dict)
    language: str | None = None
    display_name: str | None = None
    payload: dict = Field(default_factory=dict)


class IngestResult(BaseModel):
    accepted: bool
    duplicate: bool = False
    person_id: str | None = None
    event_id: str | None = None
    merged_person_ids: list[str] = Field(default_factory=list)
    review_ids: list[str] = Field(default_factory=list)
