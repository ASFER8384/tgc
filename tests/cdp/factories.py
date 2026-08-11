"""Payload builders shaped like the real thing.

Realistic Arabic and English names, +9665 numbers and SAR prices are not
decoration: a demo full of `test user 1` reads as unfinished however good the code
is, and normalisation bugs only show up against real-world formatting.
"""

import base64
import hashlib
import hmac
import json


def shopify_order(
    *,
    order_id: int = 5001,
    email: str | None = "noura.alqahtani@gmail.com",
    phone: str | None = "+966 50 123 4567",
    customer_id: int | None = 9001,
    first_name: str = "Noura",
    last_name: str = "Al Qahtani",
    total: str = "640.00",
    lines: list[tuple[str, str, int]] | None = None,
    processed_at: str = "2026-07-14T10:22:31+03:00",
    updated_at: str = "2026-07-14T10:22:31+03:00",
    source_name: str = "web",
    financial_status: str = "paid",
    locale: str = "ar-SA",
    cart_token: str | None = None,
) -> dict:
    # Per-order by default. A fixture that reused one cart token across customers
    # made three different women look like one shared browser.
    cart_token = cart_token or f"cart-token-{order_id}"
    line_items = [
        {"vendor": vendor, "price": price, "quantity": qty, "title": f"{vendor} item"}
        for vendor, price, qty in (lines or [("Aleena", "520.00", 1), ("Rawash", "120.00", 1)])
    ]
    return {
        "id": order_id,
        "email": email,
        "phone": None,
        "total_price": total,
        "currency": "SAR",
        "financial_status": financial_status,
        "processed_at": processed_at,
        "created_at": processed_at,
        "updated_at": updated_at,
        "customer_locale": locale,
        "source_name": source_name,
        "cart_token": cart_token,
        "customer": {
            "id": customer_id,
            "email": email,
            "phone": phone,
            "first_name": first_name,
            "last_name": last_name,
        },
        "shipping_address": {"phone": phone, "city": "Riyadh", "country_code": "SA"},
        "line_items": line_items,
    }


def shopify_customer(
    *,
    customer_id: int = 9002,
    email: str = "sara.otaibi@outlook.com",
    phone: str = "0501230000",
    updated_at: str = "2026-07-01T09:00:00+03:00",
) -> dict:
    return {
        "id": customer_id,
        "email": email,
        "phone": phone,
        "first_name": "Sara",
        "last_name": "Al Otaibi",
        "created_at": updated_at,
        "updated_at": updated_at,
    }


def signed(secret: str, payload: dict) -> tuple[bytes, str]:
    """Return the exact bytes to POST and their Shopify HMAC header value."""
    body = json.dumps(payload).encode()
    digest = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    return body, base64.b64encode(digest).decode()
