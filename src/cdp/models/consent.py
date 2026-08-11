from datetime import datetime

from sqlalchemy import Boolean, ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from cdp.models.base import Base, TimestampMixin, UTCDateTime, new_id

# Purpose-scoped, not a single "marketing yes/no" flag. PDPL consent is tied to a
# purpose, so agreeing to order updates on WhatsApp is not agreement to have a
# hashed phone number uploaded to Meta.
PURPOSES = (
    "marketing_email",
    "marketing_whatsapp",
    "personalization",
    "ad_audience_sharing",
)


class ConsentEvent(Base, TimestampMixin):
    """Append-only consent ledger. Current state is the latest row per
    (person, purpose) — history is never overwritten, because "prove she agreed
    on 3 March" is the question a regulator actually asks."""

    __tablename__ = "consent_events"
    __table_args__ = (Index("ix_consent_person_purpose", "person_id", "purpose", "occurred_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    person_id: Mapped[str] = mapped_column(String(32), ForeignKey("persons.id"), nullable=False)
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    granted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # Where the agreement came from: shopify_checkout, whatsapp_optin,
    # activation_form, csr_verbal... and the evidence backing it.
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
