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
from datetime import UTC, datetime, timedelta
from decimal import Decimal

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

# Real catalogue SKUs, so a seeded sale lands on stock the supplier side buys.
# Without this the orders are brand-level only and demand cannot be measured —
# which is the whole point of the two halves sharing a database.
SKU_BY_BRAND = {
    "Aleena": "ALN-SILK-NVY",
    "Rawash": "RWS-LIP-TUBE",
    "Aynola": "AYN-BOX-LUX",
}

BRAND_BY_SKU = {
    "ALN-SILK-NVY": "Aleena",
    "ALN-ABAYA-01": "Aleena",
    "RWS-LIP-TUBE": "Rawash",
    "AYN-BOX-LUX": "Aynola",
}

# Repeat purchases inside the demand window, by the same women, so the supplier
# side has something recent to measure. Anchored to midnight rather than to the
# moment the script runs: the dedupe key is built from the timestamp, so a second
# run on the same day is a no-op instead of doubling everyone's lifetime value.
#
# (customer index, sku, unit price, quantity, days ago)
RECENT_SALES = [
    (0, "ALN-SILK-NVY", "260.00", 3, 5),
    (0, "RWS-LIP-TUBE", "90.00", 2, 19),
    (1, "RWS-LIP-TUBE", "90.00", 4, 11),
    (2, "ALN-SILK-NVY", "260.00", 2, 8),
    (2, "ALN-ABAYA-01", "420.00", 1, 26),
    (3, "AYN-BOX-LUX", "150.00", 3, 15),
    (3, "RWS-LIP-TUBE", "90.00", 5, 33),
    (4, "ALN-SILK-NVY", "260.00", 4, 22),
    (4, "ALN-ABAYA-01", "420.00", 2, 40),
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
        "brand": "aleena",
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
        "brand": "aleena",
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
        "brand": "rawash",
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
                #
                # Granted per brand she actually bought from, because that is
                # where the checkout box was. And cross-brand profiling goes to
                # roughly half of them: an audience built from one brand to
                # serve another needs it, so a demo where everybody had it would
                # hide the gate that matters most here.
                if person_id and index != 1:
                    bought = {brand.lower() for brand, _, _ in orders}
                    purposes = ["marketing_whatsapp", "personalization"]
                    if index % 2 == 0:
                        purposes.append("cross_brand_profiling")
                    for brand in bought:
                        for purpose in purposes:
                            await client.post(
                                f"/persons/{person_id}/consent",
                                json={
                                    "purpose": purpose,
                                    "granted": True,
                                    "brand": brand,
                                    "source": "shopify_checkout",
                                    "evidence": "checkout opt-in checkbox",
                                },
                            )

            await self._recent_sales(client)

            # An offline mall capture, the touchpoint that is invisible without a form.
            await client.post(
                "/ingest/activation-capture",
                json={
                    "phone": "0561234567",
                    "name": "Latifa Al Shammari",
                    "event_name": "riyadh_park_popup",
                    "brand": "rawash",
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

    async def _recent_sales(self, client: httpx.AsyncClient) -> None:
        """Recent repeat orders, carrying the SKU the supplier side stocks.

        These are what the planner measures demand from. Kept separate from the
        history above so the two intents stay legible: that block exists to make
        the segments mean something, this one to make the forecast mean something.
        """
        anchor = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
        order_id = 8500
        for index, sku, unit_price, quantity, days_ago in RECENT_SALES:
            order_id += 1
            name, email, phone, locale, _ = CUSTOMERS[index]
            when = (anchor - timedelta(days=days_ago)).isoformat()
            brand = BRAND_BY_SKU[sku]
            total = str(Decimal(unit_price) * quantity)
            payload = self._order(
                order_id, 9500 + index, name, email, phone, locale, brand, total, when
            )
            payload["line_items"] = [
                {
                    "vendor": brand,
                    "sku": sku,
                    "price": unit_price,
                    "quantity": quantity,
                    "title": sku,
                }
            ]
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
        # A basket is rarely one piece. Quantity is derived from the basket value
        # and the unit price back-solved from it, so line value still sums to the
        # order total — a split that did not reconcile would corrupt every
        # lifetime-value number the segments are built on.
        quantity = max(1, int(Decimal(amount) // 200))
        unit_price = str((Decimal(amount) / quantity).quantize(Decimal("0.01")))
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
                {
                    "vendor": brand,
                    "sku": SKU_BY_BRAND.get(brand, ""),
                    "price": unit_price,
                    "quantity": quantity,
                    "title": f"{brand} item",
                }
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
