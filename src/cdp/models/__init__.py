"""Every model module must be imported here: this is the single place that
registers metadata for Alembic autogenerate and for the test schema builder."""

from cdp.models.base import Base, JSONType, TimestampMixin, new_id
from cdp.models.consent import PURPOSES, ConsentEvent
from cdp.models.event import AuditLog, Event, RawEvent
from cdp.models.person import (
    STRONG_KINDS,
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
    "PURPOSES",
    "STRONG_KINDS",
    "WEAK_KINDS",
    "ActivationDelivery",
    "ActivationRun",
    "AuditLog",
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
