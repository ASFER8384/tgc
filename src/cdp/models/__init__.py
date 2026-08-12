"""Every model module must be imported here: this is the single place that
registers metadata for Alembic autogenerate and for the test schema builder."""

from cdp.models.automation import STEP_KINDS, TRIGGERS, Automation
from cdp.models.base import Base, JSONType, TimestampMixin, new_id
from cdp.models.consent import BRANDS, PURPOSES, ConsentEvent
from cdp.models.event import AuditLog, Event, RawEvent
from cdp.models.person import (
    CAPTURE_CONTEXTS,
    STRONG_KINDS,
    THIRD_PARTY_CONTEXTS,
    WEAK_KINDS,
    Identifier,
    IdentityMerge,
    MergeReview,
    Person,
)
from cdp.models.profile import PersonBrandStat, ProfileTraits
from cdp.models.segment import (
    ActivationDelivery,
    ActivationRun,
    Segment,
    SegmentMember,
)

__all__ = [
    "BRANDS",
    "PURPOSES",
    "CAPTURE_CONTEXTS",
    "STEP_KINDS",
    "STRONG_KINDS",
    "THIRD_PARTY_CONTEXTS",
    "TRIGGERS",
    "WEAK_KINDS",
    "ActivationDelivery",
    "ActivationRun",
    "AuditLog",
    "Automation",
    "Base",
    "ConsentEvent",
    "Event",
    "Identifier",
    "IdentityMerge",
    "JSONType",
    "MergeReview",
    "Person",
    "PersonBrandStat",
    "ProfileTraits",
    "RawEvent",
    "Segment",
    "SegmentMember",
    "TimestampMixin",
    "new_id",
]
