"""Seed a trading history large enough for demand to mean something.

`seed_cdp_demo` creates five customers to make identity resolution and the
segments legible. Five customers cannot produce a credible weekly demand figure,
and a forecast measured from three sales invites exactly the objection it was
built to answer. This generates a real shape of trade instead: many customers,
repeat purchases, uneven weeks, baskets that vary.

    .venv/Scripts/python -m scripts.seed_sales_history --base-url http://127.0.0.1:8001

Everything goes in through the Shopify webhook, so identity resolution and trait
recomputation run exactly as they would in production. Order ids and timestamps
are derived from a fixed seed and from today's date, which makes a second run on
the same day a no-op rather than a doubling of everybody's lifetime value.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx

# Weight, unit price and typical basket per SKU. A lip tube sells often and in
# multiples; an embroidered abaya sells rarely and one at a time. Demand that
# came out flat across a catalogue would be the giveaway that it was invented.
CATALOGUE = [
    # sku, brand, weight, price, (min basket, max basket)
    ("RWS-LIP-TUBE", "Rawash", 46, "90.00", (2, 6)),
    ("ALN-SILK-NVY", "Aleena", 26, "260.00", (1, 3)),
    ("AYN-BOX-LUX", "Aynola", 18, "150.00", (1, 4)),
    ("ALN-ABAYA-01", "Aleena", 10, "420.00", (1, 2)),
]

FIRST_NAMES = [
    "Noura", "Sara", "Hessa", "Layla", "Mona", "Reem", "Dana", "Amal", "Huda",
    "Maha", "Ghada", "Lina", "Rana", "Salma", "Yara", "Aisha", "Fatima", "Jood",
    "Shatha", "Bushra", "Wafa", "Nadia", "Rawan", "Asma", "Haya", "Lulwa",
]
FAMILY_NAMES = [
    "Al Qahtani", "Al Otaibi", "Al Dosari", "Al Harbi", "Al Shammari",
    "Al Ghamdi", "Al Zahrani", "Al Subaie", "Al Mutairi", "Al Anazi",
    "Ibrahim", "Al Juhani", "Al Balawi", "Al Rashid",
]
DOMAINS = ["gmail.com", "outlook.com", "icloud.com", "hotmail.com"]


@dataclass(frozen=True)
class Customer:
    name: str
    email: str
    phone: str
    locale: str
    shopify_id: int


def build_customers(rng: random.Random, count: int) -> list[Customer]:
    seen: set[str] = set()
    out: list[Customer] = []
    while len(out) < count:
        first = rng.choice(FIRST_NAMES)
        family = rng.choice(FAMILY_NAMES)
        name = f"{first} {family}"
        if name in seen:
            continue
        seen.add(name)
        handle = f"{first}.{family.replace(' ', '').lower()}".lower()
        out.append(
            Customer(
                name=name,
                email=f"{handle}{len(out)}@{rng.choice(DOMAINS)}",
                # Mixed formatting on purpose: the normalisation that unifies
                # 05x, +9665x and 9665x is only exercised by messy input.
                phone=rng.choice(["05", "+9665", "9665"]) + f"{rng.randint(10, 59)}"
                + f"{rng.randint(100000, 999999)}",
                # Most of the customer base reads Arabic. A demo that is evenly
                # split misrepresents who is being sold to.
                locale="ar-SA" if rng.random() < 0.78 else "en-SA",
                shopify_id=20000 + len(out),
            )
        )
    return out


@dataclass
class Seeder:
    base_url: str
    api_key: str
    shopify_secret: str
    customers: int
    weeks: int
    orders: int
    concurrency: int

    def _signed(self, payload: dict) -> tuple[bytes, str]:
        body = json.dumps(payload).encode()
        mac = hmac.new(self.shopify_secret.encode(), body, hashlib.sha256).digest()
        return body, base64.b64encode(mac).decode()

    def _plan(self) -> dict[Customer, list[dict]]:
        rng = random.Random(20260811)
        people = build_customers(rng, self.customers)
        anchor = datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)
        skus = [row[0] for row in CATALOGUE]
        weights = [row[2] for row in CATALOGUE]
        spec = {row[0]: row for row in CATALOGUE}

        # A minority of customers place most of the orders, which is what a real
        # book of trade looks like and what makes the repeat-buyer segments
        # non-trivial.
        loyalty = [rng.random() ** 2 for _ in people]
        total = sum(loyalty)
        plan: dict[Customer, list[dict]] = {p: [] for p in people}
        order_id = 30000

        for person, share in zip(people, loyalty, strict=True):
            count = max(1, round(self.orders * share / total))
            for _ in range(count):
                sku = rng.choices(skus, weights=weights, k=1)[0]
                _, brand, _, price, (low, high) = spec[sku]
                quantity = rng.randint(low, high)
                # Weekday-weighted: the Gulf retail week peaks Thursday through
                # Saturday, and a flat spread would smooth away the shape that
                # makes a trailing average worth taking over a whole week.
                days_ago = rng.randint(0, self.weeks * 7 - 1)
                when = anchor - timedelta(
                    days=days_ago, hours=rng.randint(0, 10), minutes=rng.choice([0, 15, 30, 45])
                )
                order_id += 1
                plan[person].append(
                    {
                        "order_id": order_id,
                        "sku": sku,
                        "brand": brand,
                        "price": price,
                        "quantity": quantity,
                        "when": when,
                    }
                )
            plan[person].sort(key=lambda o: o["when"])
        return plan

    def _payload(self, person: Customer, order: dict) -> dict:
        first, _, last = person.name.partition(" ")
        total = str(Decimal(order["price"]) * order["quantity"])
        when = order["when"].isoformat()
        return {
            "id": order["order_id"],
            "email": person.email,
            "total_price": total,
            "currency": "SAR",
            "financial_status": "paid",
            "processed_at": when,
            "created_at": when,
            "updated_at": when,
            "customer_locale": person.locale,
            "source_name": "web",
            "cart_token": f"cart-{order['order_id']}",
            "customer": {
                "id": person.shopify_id,
                "email": person.email,
                "phone": person.phone,
                "first_name": first,
                "last_name": last,
            },
            "shipping_address": {
                "phone": person.phone,
                "city": "Riyadh",
                "country_code": "SA",
            },
            "line_items": [
                {
                    "vendor": order["brand"],
                    "sku": order["sku"],
                    "price": order["price"],
                    "quantity": order["quantity"],
                    "title": order["sku"],
                }
            ],
        }

    async def _post(self, client: httpx.AsyncClient, url: str, **kwargs) -> httpx.Response:
        """Retry the transient failures a long run over a remote database hits.

        A managed Postgres a continent away will drop or stall a connection
        somewhere in twelve hundred requests, and the request that lands on it
        returns a 500 that says nothing about the payload. Abandoning the whole
        seed for one blip wastes everything already ingested, so back off and
        try again; anything that fails four times in a row is a real fault and
        is allowed to raise.
        """
        delay = 1.0
        for attempt in range(4):
            try:
                response = await client.post(url, **kwargs)
                if response.status_code < 500:
                    response.raise_for_status()
                    return response
            except (httpx.TransportError, httpx.HTTPStatusError):
                if attempt == 3:
                    raise
            if attempt == 3:
                response.raise_for_status()
            await asyncio.sleep(delay)
            delay *= 2
        raise RuntimeError("unreachable")

    async def run(self) -> None:
        plan = self._plan()
        placed = sum(len(v) for v in plan.values())
        units: dict[str, int] = {}
        for orders in plan.values():
            for order in orders:
                units[order["sku"]] = units.get(order["sku"], 0) + order["quantity"]
        print(f"{len(plan)} customers, {placed} orders, {self.weeks} weeks")

        limiter = asyncio.Semaphore(self.concurrency)
        done = 0

        async with httpx.AsyncClient(base_url=self.base_url, timeout=60) as client:
            client.headers["X-API-Key"] = self.api_key

            async def place(person: Customer, orders: list[dict]) -> None:
                nonlocal done
                # One customer's orders go in sequence. Two requests resolving the
                # same identity at once race on the person row, and the loser is a
                # duplicate profile — the exact failure a CDP exists to prevent.
                async with limiter:
                    for order in orders:
                        body, mac = self._signed(self._payload(person, order))
                        response = await self._post(
                            client,
                            "/ingest/shopify",
                            content=body,
                            headers={
                                "X-Shopify-Topic": "orders/paid",
                                "X-Shopify-Hmac-Sha256": mac,
                                "Content-Type": "application/json",
                            },
                        )
                        done += 1
                        if done % 50 == 0:
                            print(f"  {done}/{placed}")
                    # Consent for most, not all: the gate has to be visible doing
                    # something or it reads as decoration.
                    # Keyed on the stable id, not on hash(): PYTHONHASHSEED is
                    # randomised per process, so a rerun would silently grant
                    # consent to a different set of people than the first run.
                    if person.shopify_id % 10 < 8:
                        person_id = response.json().get("person_id")
                        if person_id:
                            # One grant per brand she bought from — the checkout
                            # box belongs to that brand's checkout. Cross-brand
                            # profiling goes to a subset, so the audiences that
                            # need it are visibly smaller than those that do not.
                            bought = {o["brand"].lower() for o in orders}
                            purposes = ["marketing_whatsapp", "personalization"]
                            if person.shopify_id % 2 == 0:
                                purposes.append("cross_brand_profiling")
                            for brand in sorted(bought):
                                for purpose in purposes:
                                    await self._post(
                                        client,
                                        f"/persons/{person_id}/consent",
                                        json={
                                            "purpose": purpose,
                                            "granted": True,
                                            "brand": brand,
                                            "source": "shopify_checkout",
                                            "evidence": "checkout opt-in checkbox",
                                        },
                                    )

            await asyncio.gather(*(place(p, o) for p, o in plan.items() if o))

        print("\nunits sold, and what that is per week:")
        for sku, count in sorted(units.items(), key=lambda kv: -kv[1]):
            print(f"  {sku:<14} {count:>6} units   {count / self.weeks:>7.1f}/wk")


def main() -> None:
    from cdp.config import get_settings as cdp_settings
    from sca.config import get_settings as platform_settings

    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    parser.add_argument("--api-key", default=platform_settings().api_key)
    parser.add_argument(
        "--shopify-secret", default=cdp_settings().shopify_webhook_secret or "dev-shopify-secret"
    )
    parser.add_argument("--customers", type=int, default=60)
    parser.add_argument("--weeks", type=int, default=10)
    parser.add_argument("--orders", type=int, default=600)
    parser.add_argument("--concurrency", type=int, default=4)
    args = parser.parse_args()
    asyncio.run(
        Seeder(
            args.base_url,
            args.api_key,
            args.shopify_secret,
            args.customers,
            args.weeks,
            args.orders,
            args.concurrency,
        ).run()
    )


if __name__ == "__main__":
    main()
