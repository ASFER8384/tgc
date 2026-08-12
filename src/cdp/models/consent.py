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
    # Permission for one brand to use what another brand knows. Separate from
    # personalization on purpose: agreeing that Aleena may tailor its own store
    # to your Aleena history is not agreeing that Aleena may read your Rawash
    # basket. Without this grant a cross-brand audience cannot include you.
    "cross_brand_profiling",
)

# The portfolio. A grant belongs to one of these, never to "the company": she
# bought from Aleena and said yes to Aleena, and Rawash was not party to that
# conversation. The whole point of scoping consent is that this stays true even
# though one profile spans all three.
BRANDS = ("aleena", "rawash", "aynola")


class ConsentEvent(Base, TimestampMixin):
    """Append-only consent ledger. Current state is the latest row per
    (person, purpose) — history is never overwritten, because "prove she agreed
    on 3 March" is the question a regulator actually asks."""

    __tablename__ = "consent_events"
    __table_args__ = (
        Index("ix_consent_person_purpose", "person_id", "purpose", "occurred_at"),
        Index("ix_consent_person_brand", "person_id", "brand", "purpose", "occurred_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    person_id: Mapped[str] = mapped_column(String(32), ForeignKey("persons.id"), nullable=False)
    # The brand the agreement was made with. Nullable only because grants
    # recorded before scoping existed cannot be attributed honestly after the
    # fact — and an unattributable grant authorises nothing, so NULL never
    # satisfies a brand-scoped gate. Guessing here would be inventing consent.
    brand: Mapped[str | None] = mapped_column(String(64))
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    granted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # Where the agreement came from: shopify_checkout, whatsapp_optin,
    # activation_form, csr_verbal... and the evidence backing it.
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
