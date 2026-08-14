"""Importing this package registers every table on the shared metadata.

A new model module must be added here or Alembic autogenerate will silently miss
its table, and the schema parity test will fail with a diff nobody expects.
"""

from sca.models.base import Base, JSONType, TimestampMixin, UTCDateTime, new_id
from sca.models.issue import ISSUE_KINDS, SEVERITIES, AuditLog, Issue
from sca.models.message import MESSAGE_KINDS, Attachment, InboundMessage
from sca.models.order import (
    ALLOWED_TRANSITIONS,
    STATUSES,
    Document,
    PurchaseOrder,
    PurchaseOrderLine,
    Shipment,
)
from sca.models.setting import AppSetting
from sca.models.supplier import (
    CHANNELS,
    Item,
    StockLevel,
    StockSnapshot,
    Supplier,
    SupplierItem,
)

__all__ = [
    "ALLOWED_TRANSITIONS",
    "CHANNELS",
    "ISSUE_KINDS",
    "MESSAGE_KINDS",
    "SEVERITIES",
    "STATUSES",
    "AppSetting",
    "Attachment",
    "AuditLog",
    "Base",
    "Document",
    "InboundMessage",
    "Issue",
    "Item",
    "JSONType",
    "PurchaseOrder",
    "PurchaseOrderLine",
    "Shipment",
    "StockLevel",
    "StockSnapshot",
    "Supplier",
    "SupplierItem",
    "TimestampMixin",
    "UTCDateTime",
    "new_id",
]
