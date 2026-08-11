"""Seed a demo dataset: realistic customers across Aleena / Rawash / Aynola.

Run against a live API so it exercises the same code path a real connector does —
webhook in, identity resolved, traits recomputed — rather than writing rows the
application would never produce.

    .venv/Scripts/python -m scripts.seed_demo --base-url http://localhost:8000

Arabic and English names, +9665 numbers, SAR prices and Ramadan/Eid seasonality
are deliberate: a demo full of "test user 1" reads as unfinished, and the
normalisation paths that matter only fire against real-world formatting.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import json
from dataclasses import dataclass

import httpx

CUSTOMERS = [
    # (name, email, phone, locale, orders as (brand, amount, when))
    (
        "Noura Al Qahtani",
        "Noura.AlQahtani+shop@gmail.com",
        "+966 50 123 4567",
        "ar-SA",
        [
            ("Aleena", "520.00", "2026-03-14T10:22:31+03:00"),
            ("Aleena", "780.00", "2026-06-02T19:40:00+03:00"),
        ],
    ),
    (
        "Sara Al Otaibi",
        "sara.otaibi@outlook.com",
        "0501230000",
        "ar-SA",
        [("Rawash", "180.00", "2026-04-09T12:10:00+03:00")],
    ),
    (
        "Hessa Al Dosari",
        "hessa@gmail.com",
        "0555000111",
        "ar-SA",
        [
            ("Aleena", "640.00", "2026-07-01T15:00:00+03:00"),
            ("Aleena", "460.00", "2026-07-28T11:30:00+03:00"),
        ],
    ),
    (
        "Layla Ibrahim",
        "layla.ibrahim@icloud.com",
        "0533221100",
        "en-SA",
        [
            ("Aynola", "95.00", "2026-05-21T09:05:00+03:00"),
            ("Rawash", "240.00", "2026-06-30T21:15:00+03:00"),
        ],
    ),
    (
        "Mona Al Harbi",
        "mona.alharbi@gmail.com",
        "0544332211",
        "ar-SA",
        [("Aleena", "890.00", "2026-02-18T17:45:00+03:00")],
    ),
]

SEGMENTS = [
    {
        "key": "aleena_no_rawash",
        "name": "Aleena buyers who have never tried Rawash",
        "description": "The cross-brand upsell the three-brand portfolio exists to capture.",
        "definition": {
            "all": [
                {"brand_purchased": "aleena"},
                {"brand_not_purchased": "rawash"},
                {"trait": "aov", "op": "gte", "value": 400},
            ]
        },
        "required_consent": "marketing_whatsapp",
    },
    {
        "key": "lapsing_high_value",
        "name": "High value, drifting away",
        "description": "Spent over SAR 1000 and has not ordered in 60 days.",
        "definition": {
            "all": [
                {"trait": "ltv", "op": "gte", "value": 1000},
                {"trait": "recency_days", "op": "gte", "value": 60},
            ]
        },
        "required_consent": "marketing_whatsapp",
    },
    {
        "key": "arabic_speaking_repeat",
        "name": "Arabic-speaking repeat customers",
        "definition": {
            "all": [
                {"trait": "preferred_language", "op": "eq", "value": "ar"},
                {"trait": "order_count", "op": "gte", "value": 2},
            ]
        },
        "required_consent": "personalization",
    },
]


@dataclass
class Seeder:
    base_url: str
    api_key: str
    shopify_secret: str

    def _signed(self, payload: dict) -> tuple[bytes, str]:
        body = json.dumps(payload).encode()
        mac = hmac.new(self.shopify_secret.encode(), body, hashlib.sha256).digest()
        return body, base64.b64encode(mac).decode()

    async def run(self) -> None:
        async with httpx.AsyncClient(base_url=self.base_url, timeout=30) as client:
            client.headers["X-API-Key"] = self.api_key
            order_id = 8000
            for index, (name, email, phone, locale, orders) in enumerate(CUSTOMERS):
                customer_id = 9500 + index
                person_id: str | None = None
                for brand, amount, when in orders:
                    order_id += 1
                    payload = self._order(
                        order_id, customer_id, name, email, phone, locale, brand, amount, when
                    )
                    body, mac = self._signed(payload)
                    response = await client.post(
                        "/ingest/shopify",
                        content=body,
                        headers={
                            "X-Shopify-Topic": "orders/paid",
                            "X-Shopify-Hmac-Sha256": mac,
                            "Content-Type": "application/json",
                        },
                    )
                    response.raise_for_status()
                    person_id = response.json()["person_id"]

                # WhatsApp conversation for the same woman, keyed on her phone
                # only — this is the stitch the demo turns on.
                await client.post(
                    "/ingest/event",
                    json={
                        "source": "whatsapp",
                        "name": "message_in",
                        "dedupe_key": f"wa:seed:{customer_id}",
                        "occurred_at": "2026-07-30T09:15:00+03:00",
                        "identifiers": {"phone": phone},
                        "channel": "whatsapp",
                        "payload": {"text": "هل يوجد مقاس M؟"},
                    },
                )

                # Most, not all, have opted in — a demo where everyone consented
                # cannot show the consent gate doing anything.
                if person_id and index != 1:
                    for purpose in ("marketing_whatsapp", "personalization"):
                        await client.post(
                            f"/persons/{person_id}/consent",
                            json={
                                "purpose": purpose,
                                "granted": True,
                                "source": "shopify_checkout",
                                "evidence": "checkout opt-in checkbox",
                            },
                        )

            # An offline mall capture, the touchpoint that is invisible without a form.
            await client.post(
                "/ingest/activation-capture",
                json={
                    "phone": "0561234567",
                    "name": "Latifa Al Shammari",
                    "event_name": "riyadh_park_popup",
                    "brand_interest": "rawash",
                    "language": "ar",
                    "consent_marketing_whatsapp": True,
                },
            )

            for segment in SEGMENTS:
                response = await client.post("/segments", json=segment)
                response.raise_for_status()
                evaluated = await client.post(f"/segments/{segment['key']}/evaluate")
                print(f"{segment['key']:>24}: {evaluated.json()['size']} members")

    def _order(
        self,
        order_id: int,
        customer_id: int,
        name: str,
        email: str,
        phone: str,
        locale: str,
        brand: str,
        amount: str,
        when: str,
    ) -> dict:
        first, _, last = name.partition(" ")
        return {
            "id": order_id,
            "email": email,
            "total_price": amount,
            "currency": "SAR",
            "financial_status": "paid",
            "processed_at": when,
            "created_at": when,
            "updated_at": when,
            "customer_locale": locale,
            "source_name": "web",
            "cart_token": f"cart-{order_id}",
            "customer": {
                "id": customer_id,
                "email": email,
                "phone": phone,
                "first_name": first,
                "last_name": last,
            },
            "shipping_address": {"phone": phone, "city": "Riyadh", "country_code": "SA"},
            "line_items": [
                {"vendor": brand, "price": amount, "quantity": 1, "title": f"{brand} item"}
            ],
        }


def main() -> None:
    # Defaults come from the running configuration rather than from literals: the
    # two halves share one key and one Shopify secret now, and a script that
    # guessed them would fail with a 401 that looks like a bug in the API.
    from cdp.config import get_settings as cdp_settings
    from sca.config import get_settings as platform_settings

    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    parser.add_argument("--api-key", default=platform_settings().api_key)
    parser.add_argument(
        "--shopify-secret", default=cdp_settings().shopify_webhook_secret or "dev-shopify-secret"
    )
    args = parser.parse_args()
    asyncio.run(Seeder(args.base_url, args.api_key, args.shopify_secret).run())


if __name__ == "__main__":
    main()
