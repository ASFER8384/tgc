import base64
import hashlib
import hmac
from datetime import UTC, datetime
from decimal import Decimal

from cdp.config import get_settings
from cdp.ingest.schemas import CanonicalEvent

# Shopify vendor / product-tag values mapped to TGC brands. Vendor is the field
# the merchandising team already maintains, so this stays a lookup rather than a
# guess; unknown vendors fall through to "unassigned" and are visible in the
# console instead of being silently dropped.
BRAND_BY_VENDOR = {
    "aleena": "aleena",
    "rawash": "rawash",
    "aynola": "aynola",
}


def _brand_map() -> dict[str, str]:
    """The three known names, plus whatever the environment adds.

    Merged rather than replaced: a store connected later should not have to
    restate the brands that were already working, and a typo in one pair should
    not silently unmap the others.
    """
    mapping = dict(BRAND_BY_VENDOR)
    for pair in (get_settings().brand_by_vendor or "").split(","):
        vendor, _, brand = pair.partition("=")
        vendor, brand = vendor.strip().lower(), brand.strip().lower()
        if vendor and brand:
            mapping[vendor] = brand
    return mapping


SUPPORTED_TOPICS = (
    "orders/paid",
    "orders/create",
    "customers/create",
    "customers/update",
    "checkouts/create",
)


def verify_webhook(secret: str, body: bytes, header_hmac: str | None) -> bool:
    """Shopify signs the raw body with HMAC-SHA256, base64 encoded.

    Verification runs on the *bytes* as received — re-serialising the JSON first
    changes the digest and is the usual reason this check "mysteriously" fails.
    Compared in constant time.
    """
    if not secret or not header_hmac:
        return False
    digest = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode()
    return hmac.compare_digest(expected, header_hmac)


def _parse_dt(value: str | None) -> datetime:
    if not value:
        return datetime.now(UTC)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(UTC)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _identifiers(payload: dict, *, is_customer_payload: bool) -> dict[str, str | None]:
    # On customers/* topics the payload IS the customer; on order topics the
    # customer is nested. Getting this backwards silently indexes an order id as
    # a customer id, which fragments every profile it touches.
    customer = payload if is_customer_payload else (payload.get("customer") or {})
    shipping = payload.get("shipping_address") or customer.get("default_address") or {}
    billing = payload.get("billing_address") or {}
    customer_id = customer.get("id")

    return {
        "email": payload.get("email") or customer.get("email"),
        # Order of preference matters: a customer-level phone is the person's own,
        # while a shipping phone can belong to whoever receives the parcel.
        "phone": (
            customer.get("phone")
            or payload.get("phone")
            or shipping.get("phone")
            or billing.get("phone")
        ),
        "shopify_customer_id": str(customer_id) if customer_id else None,
        # Shopify's client-side id, present when the storefront pixel is wired up.
        "device_id": (payload.get("client_details") or {}).get("browser_ip_hash"),
        # A cart token identifies one checkout, not one device. Kept separate
        # because conflating them made a customer with forty orders appear to own
        # forty devices, and because the two are useful for different things: a
        # device persists across visits, a cart token links an abandoned checkout
        # to the order that follows it and is then never seen again.
        "cart_token": payload.get("cart_token"),
    }


def _brand_split(payload: dict) -> dict[str, Decimal]:
    mapping = _brand_map()
    # What an unrecognised vendor becomes. On a multi brand store "unassigned"
    # is the right answer and is meant to be noticed; on a single brand store
    # every line would read that way, so the environment can name the brand.
    fallback = (get_settings().default_brand or "unassigned").strip().lower()
    split: dict[str, Decimal] = {}
    for line in payload.get("line_items") or []:
        vendor = (line.get("vendor") or "").strip().lower()
        brand = mapping.get(vendor, fallback)
        price = Decimal(str(line.get("price") or "0")) * int(line.get("quantity") or 1)
        discount = sum(
            (Decimal(str(d.get("amount") or "0")) for d in line.get("discount_allocations") or []),
            Decimal("0"),
        )
        split[brand] = split.get(brand, Decimal("0")) + price - discount
    return {k: v.quantize(Decimal("0.01")) for k, v in split.items() if v > 0}


def _display_name(payload: dict) -> str | None:
    customer = payload.get("customer") or payload
    parts = [customer.get("first_name"), customer.get("last_name")]
    name = " ".join(p for p in parts if p).strip()
    return name or None


def to_canonical(topic: str, payload: dict) -> CanonicalEvent | None:
    """Translate one Shopify webhook into the canonical vocabulary.

    Offline sales are not a separate integration: an order created through
    Shopify POS arrives on the same topic with ``source_name`` set to ``pos``, so
    in-store and online unify for free. A different POS in the retail store would
    need its own connector — that is the single biggest scoping question on this
    project.
    """
    if topic not in SUPPORTED_TOPICS:
        return None

    native_id = payload.get("id") or payload.get("token") or payload.get("admin_graphql_api_id")
    if native_id is None:
        return None
    updated = payload.get("updated_at") or payload.get("created_at") or ""
    # updated_at is part of the key so a genuine later edit to the same order is
    # ingested, while a redelivery of the identical state is not.
    dedupe_key = f"shopify:{topic}:{native_id}:{updated}"

    source = "shopify_pos" if (payload.get("source_name") or "") == "pos" else "shopify"
    is_customer_payload = topic.startswith("customers/")
    common = {
        "source": source,
        "dedupe_key": dedupe_key,
        "identifiers": _identifiers(payload, is_customer_payload=is_customer_payload),
        "display_name": _display_name(payload),
        "language": (payload.get("customer_locale") or "").split("-")[0] or None,
        # order_id is promoted out of the source payload so a refund can name the
        # order it reverses without every consumer knowing Shopify's field names.
        "payload": {**payload, "order_id": str(native_id)},
    }

    if topic in {"orders/paid", "orders/create"}:
        # An unpaid order is not revenue. Only orders/paid feeds LTV; orders/create
        # is kept as timeline context, at zero value.
        is_paid = topic == "orders/paid" or payload.get("financial_status") == "paid"
        total = Decimal(str(payload.get("total_price") or "0"))
        return CanonicalEvent(
            name="order_paid" if is_paid else "checkout_started",
            occurred_at=_parse_dt(payload.get("processed_at") or payload.get("created_at")),
            value_amount=total if is_paid else None,
            currency=payload.get("currency") or "SAR",
            channel="retail" if source == "shopify_pos" else "online",
            brands=_brand_split(payload) if is_paid else {},
            **common,
        )

    if topic == "checkouts/create":
        return CanonicalEvent(
            name="checkout_started",
            occurred_at=_parse_dt(payload.get("created_at")),
            currency=payload.get("currency") or "SAR",
            **common,
        )

    # customers/create | customers/update — no behaviour, but the identifiers on
    # it are what let a later anonymous session or POS sale attach to a person.
    return CanonicalEvent(
        name="lead_captured",
        occurred_at=_parse_dt(payload.get("updated_at") or payload.get("created_at")),
        **common,
    )
