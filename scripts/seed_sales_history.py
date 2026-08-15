"""Seed a trading history with a shape in it.

The first version of this scattered orders uniformly across the window —
``days_ago = randint(0, weeks * 7 - 1)`` — which makes every week the same week
plus noise. That is a flat rate, and for a flat rate the trailing average is not
merely hard to beat, it is *optimal*: anything that beats it on the test weeks
has fitted noise and will lose on the next ones. The forecast module spent three
runs correctly reporting that there was nothing to learn, which is the right
answer to the wrong data.

So this generates trade with structure a forecast can actually find, all of it
the kind a Saudi fashion group really has:

* **Customer rhythm.** Each customer has a cadence — every three weeks, every
  four months — and a bias towards particular lines. This is what makes
  "who will buy" answerable at all, and what ``weeks_since_bought`` and the
  per-person features are for.
* **The Eid run-up.** Abayas and silk climb for six weeks before Eid and fall
  off a cliff the week after; beauty peaks tighter and later. Two Eids fall
  inside a two-year window, so the pattern repeats — one occurrence is an
  anecdote and cannot be learned from.
* **Summer.** Riyadh in July is quiet, and quiet is not the same as declining.
* **Trend.** Silk is growing, the abaya line is slowly being retired, the gift
  box is flat. A trailing average cannot see a slope; that is the plainest
  advantage a model has.
* **The weekday.** Thursday to Saturday carries the week. The old comment
  claimed this and the code did not do it.
* **Stockouts, and a ledger to prove them.** Two planned outages where demand
  existed and sales could not happen, with weekly stock readings posted so the
  panel can tell an empty shelf from a quiet week rather than learning that
  nobody wanted the thing.

    .venv/Scripts/python -m scripts.seed_sales_history --base-url http://127.0.0.1:8101

Everything goes in through the Shopify webhook, so identity resolution and trait
recomputation run exactly as they would in production.

**Re-running is safe.** Orders sit on absolute dates from a fixed start, not on
"days ago", so the same order carries the same timestamp whenever the script is
run and the ingest dedupe key collapses it. A second run this week is a no-op; a
run next week adds next week and leaves everything before it alone. The old
version anchored on today, which meant running it twice on different days
silently doubled the history.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import math
import random
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import httpx

# Weight, unit price and typical basket per SKU. A lip tube sells often and in
# multiples; an embroidered abaya sells rarely and one at a time. Demand that
# came out flat across a catalogue would be the giveaway that it was invented.
#
# ``trend`` is the compounding weekly drift: silk is growing, the abaya line is
# being retired, the gift box holds. ``eid`` is how hard that line lifts into the
# holiday, and ``lull`` how far it falls away over high summer.
CATALOGUE = [
    # sku, brand, weight, price, (min basket, max basket), trend, eid, lull
    ("RWS-LIP-TUBE", "Rawash", 46, "90.00", (2, 6), 1.0015, 1.9, 0.85),
    ("ALN-SILK-NVY", "Aleena", 26, "260.00", (1, 3), 1.0060, 2.6, 0.70),
    ("AYN-BOX-LUX", "Aynola", 18, "150.00", (1, 4), 1.0000, 1.6, 0.90),
    ("ALN-ABAYA-01", "Aleena", 10, "420.00", (1, 2), 0.9945, 3.1, 0.60),
]

# Where each line actually sells, as (online, store, mall). A bolt of silk wants
# touching before it is bought and an abaya more so; a gift box is mostly ordered
# for somebody else and arrives online. Demand that came out of one channel would
# make the unified profile a claim rather than a demonstration — the whole point
# is that a woman who buys online in March and at the counter in June is one
# customer with one history.
# The counter and the mall stand, and nothing online.
#
# Aleena's storefront is a live Shopify store sending real webhooks, so its
# orders are not invented here: seeding that channel would mix made-up baskets
# into the one source of genuine trade in the database, and from then on nobody
# could say which figure came from a customer. The two brands with no storefront
# are the ones that need seeding, and Aleena's counter and mall trade is real
# trade that its storefront never sees either.
#
# The online column is kept at zero rather than removed. When Rawash or Aynola
# open a storefront, or a demo needs a self-contained history with no live store
# behind it, this is the one number to change.
CHANNEL_MIX = {
    "RWS-LIP-TUBE": (0.00, 0.62, 0.38),
    "ALN-SILK-NVY": (0.00, 0.82, 0.18),
    "AYN-BOX-LUX": (0.00, 0.80, 0.20),
    "ALN-ABAYA-01": (0.00, 0.90, 0.10),
}

# How often the counter and the mall stand get a name and a number out of the
# customer. Online always has one; a shop assistant asks and is sometimes told,
# and a stand in a mall on a Thursday evening is mostly footfall. The sales that
# get neither land on the till's standing walk-in record, count towards what the
# shop sold, and are honestly missing from "who will buy".
CAPTURE = {"store": 0.7, "mall": 0.55}

# Eid al-Fitr, the week the selling stops. Two inside a two-year window, which is
# the minimum for a season to be a pattern rather than an anecdote.
EIDS = (date(2025, 3, 30), date(2026, 3, 20), date(2027, 3, 9))

# How the retail week is actually shaped. Monday is 0.
WEEKDAY_WEIGHT = (0.9, 0.85, 0.95, 1.45, 1.6, 1.5, 0.75)

# Where the shelf went empty. Demand carried on and sales could not, which is the
# case the panel has to be able to recognise — sales of zero with stock of zero
# says nothing about what customers wanted.
OUTAGES = (
    ("ALN-SILK-NVY", date(2025, 6, 2), date(2025, 6, 30)),
    ("RWS-LIP-TUBE", date(2026, 1, 5), date(2026, 1, 26)),
)

FIRST_NAMES = [
    "Noura", "Sara", "Hessa", "Layla", "Mona", "Reem", "Dana", "Amal", "Huda",
    "Maha", "Ghada", "Lina", "Rana", "Salma", "Yara", "Aisha", "Fatima", "Jood",
    "Shatha", "Bushra", "Wafa", "Nadia", "Rawan", "Asma", "Haya", "Lulwa",
    "Munira", "Areej", "Jawaher", "Basma", "Nouf", "Tala", "Raghad", "Shahad",
]
FAMILY_NAMES = [
    "Al Qahtani", "Al Otaibi", "Al Dosari", "Al Harbi", "Al Shammari",
    "Al Ghamdi", "Al Zahrani", "Al Subaie", "Al Mutairi", "Al Anazi",
    "Ibrahim", "Al Juhani", "Al Balawi", "Al Rashid", "Al Amri", "Al Malki",
]
DOMAINS = ["gmail.com", "outlook.com", "icloud.com", "hotmail.com"]

# How often each box is actually ticked. Marketing is the one most people
# accept; profiling across the group is a bigger ask and a smaller number say
# yes, which is the whole reason the cross-brand gate exists — if everybody
# granted it the audiences that need it would be the same size as the ones that
# do not, and the gate would be decoration. Some tick nothing at all.
CONSENT_RATE = {
    "marketing_whatsapp": 0.74,
    "personalization": 0.61,
    "cross_brand_profiling": 0.33,
}

# What a line actually comes in, and how the mix splits. A buyer does not order
# "forty abayas", she orders eight in a 54 and two in a 60, and a forecast that
# only reaches the SKU leaves the hardest half of the decision unanswered. The
# weights are deliberately lopsided — the middle sizes carry the line and the
# ends are where the money is lost.
VARIANTS = {
    "RWS-LIP-TUBE": [
        ("Rose Nude", 34), ("Deep Berry", 27), ("Coral", 21), ("Classic Red", 18),
    ],
    "ALN-SILK-NVY": [("S", 14), ("M", 33), ("L", 31), ("XL", 16), ("XXL", 6)],
    "AYN-BOX-LUX": [("Small", 38), ("Large", 62)],
    "ALN-ABAYA-01": [("52", 11), ("54", 26), ("56", 29), ("58", 21), ("60", 13)],
}

# When the group discounts, and by how much. Without variation in price there is
# nothing for an elasticity to be measured against — every unit ever sold at the
# same number teaches only that number. These are the three moments a Saudi
# fashion group actually marks down.
def _discount(week: date) -> float:
    """Share off the list price this week, as a fraction."""
    # White Friday, and the week after it while stock clears.
    if week.month == 11 and week.day >= 22:
        return 0.30
    if week.month == 12 and week.day <= 7:
        return 0.20
    # End-of-season clearance in January.
    if week.month == 1 and 7 <= week.day <= 28:
        return 0.20
    # A softer promotion into Eid — the demand is there anyway, so the discount
    # is smaller and it is about share rather than volume.
    for eid_day in EIDS:
        weeks_to = (_monday(eid_day) - week).days / 7.0
        if 1 <= weeks_to <= 3:
            return 0.10
    return 0.0


SKU_BRAND = {row[0]: row[1] for row in CATALOGUE}
BRANDS = sorted({row[1] for row in CATALOGUE})
# How much of the group each brand carries, from the catalogue weights. A
# customer picked uniformly across three brands would make the smallest one as
# common as the largest.
BRAND_WEIGHT = {
    brand: sum(row[2] for row in CATALOGUE if row[1] == brand) for brand in BRANDS
}


def _monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


# The mall stand is not always there. It goes up for the Eid run-up, for the
# National Day weekend and for the late-November trade, and the rest of the year
# there is no mall channel at all — a pop-up that runs fifty-two weeks a year is
# a shop.
def _activation_weeks() -> set[date]:
    weeks: set[date] = set()
    for eid_day in EIDS:
        peak = _monday(eid_day)
        for back in range(1, 5):
            weeks.add(peak - timedelta(weeks=back))
    for year in (2024, 2025, 2026, 2027):
        weeks.add(_monday(date(year, 9, 23)))
        weeks.add(_monday(date(year, 11, 25)))
    return weeks


def _poisson(rng: random.Random, mean: float) -> int:
    """Knuth's method. Small counts, so the loop is short and the standard
    library not having this is not worth a dependency over."""
    if mean <= 0:
        return 0
    limit, count, product = math.exp(-mean), 0, 1.0
    while True:
        count += 1
        product *= rng.random()
        if product <= limit:
            return count - 1


def _season(week: date, eid: float, lull: float) -> float:
    """What this week does to demand for a line, before trend.

    Two effects, multiplied. The holiday: a six-week climb into Eid, then a
    collapse the week after — people buy for the holiday and then stop, and a
    model that learns the climb without the cliff will over-buy every spring.
    And the summer, which is a level shift rather than a spike.
    """
    factor = 1.0
    for eid_day in EIDS:
        weeks_to = (_monday(eid_day) - week).days / 7.0
        if 0 <= weeks_to <= 6:
            # Nearest week is the peak; six weeks out is barely lifted.
            factor *= 1.0 + (eid - 1.0) * ((6 - weeks_to) / 6.0) ** 1.8
        elif -3 <= weeks_to < 0:
            factor *= 0.55 + 0.15 * (-weeks_to)
    # July and August, when Riyadh empties.
    if week.month in (7, 8):
        factor *= lull
    return factor


# Compared by identity, not by field: the plan is keyed on the customer and a
# dataclass with a dict field is otherwise unhashable.
@dataclass(eq=False)
class Customer:
    name: str
    email: str
    phone: str
    locale: str
    shopify_id: int
    # The week she first bought. Staggered acquisition is what gives tenure and
    # the "new versus returning" split anything to say.
    joined: date
    # Weeks between purchases, on average. A regular is 3, a once-a-season
    # customer is 16, and the gap between them is most of what "who will buy"
    # is reading.
    cadence: float
    # How she splits across the catalogue. A customer who buys lipstick and a
    # customer who buys abayas are not the same customer with different luck.
    affinity: dict[str, float] = field(default_factory=dict)
    # Which boxes she ticked at the checkout. Decided once, here, rather than at
    # send time: a re-run has to grant the same consent to the same people or the
    # audiences move underneath the segments every time the seed is touched.
    consents: list[str] = field(default_factory=list)


def build_customers(rng: random.Random, count: int, start: date, end: date) -> list[Customer]:
    seen: set[str] = set()
    out: list[Customer] = []
    weeks = max(1, (end - start).days // 7)
    skus = [row[0] for row in CATALOGUE]
    base_weight = {row[0]: float(row[2]) for row in CATALOGUE}
    while len(out) < count:
        first = rng.choice(FIRST_NAMES)
        family = rng.choice(FAMILY_NAMES)
        name = f"{first} {family}"
        if name in seen:
            continue
        seen.add(name)
        handle = f"{first}.{family.replace(' ', '').lower()}".lower()
        # Most of the book was acquired early, with a steady trickle since —
        # a cohort curve rather than a uniform sprinkle, so tenure varies.
        joined = start + timedelta(weeks=int(weeks * rng.random() ** 1.7))
        # A minority buy often. Squaring a uniform draw puts most people at a
        # long cadence and a few at a short one, which is what a real book of
        # trade looks like and what makes the repeat-buyer segments non-trivial.
        cadence = 2.0 + 20.0 * (rng.random() ** 1.6)
        # Which brands she buys at all. Most people buy one — a Rawash customer
        # is a Rawash customer — a third have crossed to a second, and a handful
        # buy across the group. Giving everybody all three would make the
        # cross-brand consent gate guard nothing and "who might cross over" a
        # question with the same answer for every customer on the list.
        draw = rng.random()
        breadth = 1 if draw < 0.62 else 2 if draw < 0.92 else 3
        # Weighted by how big each brand actually is, so the home brand is
        # usually the one that sells the most rather than a coin flip.
        home = rng.choices(BRANDS, weights=[BRAND_WEIGHT[b] for b in BRANDS], k=1)[0]
        repertoire = [home]
        for brand in rng.sample([b for b in BRANDS if b != home], k=len(BRANDS) - 1):
            if len(repertoire) < breadth:
                repertoire.append(brand)
        affinity = {
            sku: base_weight[sku] * (0.15 + rng.random() ** 2 * 2.4)
            for sku in skus
            if SKU_BRAND[sku] in repertoire
        }
        # The brand she came for outsells the one she crossed to.
        for sku in affinity:
            if SKU_BRAND[sku] != home:
                affinity[sku] *= 0.45
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
                # Deliberately clear of the 20000 range the first version used.
                # A Shopify customer id is a strong identifier: reusing one with a
                # different email would resolve the two into a single person and
                # merge two invented customers into one corrupt profile.
                shopify_id=40000 + len(out),
                consents=[
                    purpose for purpose, chance in CONSENT_RATE.items() if rng.random() < chance
                ],
                joined=joined,
                cadence=cadence,
                affinity=affinity,
            )
        )
    return out


@dataclass
class Seeder:
    base_url: str
    api_key: str
    shopify_secret: str
    customers: int
    start: date
    end: date
    concurrency: int
    with_stock: bool

    def _signed(self, payload: dict) -> tuple[bytes, str]:
        body = json.dumps(payload).encode()
        mac = hmac.new(self.shopify_secret.encode(), body, hashlib.sha256).digest()
        return body, base64.b64encode(mac).decode()

    def _weeks(self) -> list[date]:
        out, week = [], self.start
        while week < self.end:
            out.append(week)
            week += timedelta(weeks=1)
        return out

    def _out_of_stock(self, sku: str, week: date) -> bool:
        return any(s == sku and begins <= week < ends for s, begins, ends in OUTAGES)

    def _plan(self) -> tuple[dict[Customer, list[dict]], dict[str, dict[date, int]]]:
        rng = random.Random(20260811)
        weeks = self._weeks()
        people = build_customers(rng, self.customers, self.start, self.end)
        spec = {row[0]: row for row in CATALOGUE}

        activations = _activation_weeks()
        plan: dict[Customer, list[dict]] = {p: [] for p in people}
        # Demand, not sales: what the shelf was asked for, including the weeks it
        # could not answer. The stock simulation needs the first; the orders that
        # go in are the second.
        demand: dict[str, dict[date, int]] = {row[0]: dict.fromkeys(weeks, 0) for row in CATALOGUE}
        # Clear of the first version's 30000s for the same reason: a second
        # order carrying an id the platform has already seen is an edit, not a
        # sale.
        order_id = 100000

        for person in people:
            skus = list(person.affinity)
            for week in weeks:
                if week < person.joined:
                    continue
                age = (week - person.joined).days / 7.0
                # The chance she buys at all this week: her own rhythm, lifted or
                # flattened by the season on whatever she tends to buy. A rate
                # rather than a countdown, so the gap between orders varies the
                # way a real one does instead of ticking like a metronome.
                weights = [person.affinity[s] for s in skus]
                seasonal = sum(
                    w * _season(week, spec[s][6], spec[s][7]) for s, w in zip(skus, weights,
                                                                             strict=True)
                ) / sum(weights)
                rate = (1.0 / person.cadence) * seasonal
                # New customers buy sooner after joining than their long-run
                # cadence suggests, then settle.
                if age < 4:
                    rate *= 1.6
                markdown = _discount(week)
                # A discount does not only change the price, it changes how many
                # people buy — that relationship is the whole point of recording
                # the price, and a seed where markdowns move nothing would teach
                # an elasticity of zero.
                for _ in range(_poisson(rng, rate * (1.0 + 1.8 * markdown))):
                    sku = rng.choices(skus, weights=weights, k=1)[0]
                    _, brand, _, price, (low, high), trend, eid, lull = spec[sku]
                    quantity = rng.randint(low, high)
                    if markdown >= 0.2 and rng.random() < 0.35:
                        quantity += 1  # people stock up in a real sale
                    names, sizes = zip(*VARIANTS[sku], strict=True)
                    variant = rng.choices(names, weights=sizes, k=1)[0]
                    paid = (Decimal(price) * (Decimal(1) - Decimal(str(markdown)))
                            ).quantize(Decimal("0.01"))
                    # The line's own drift, compounded from the start of history.
                    if rng.random() > min(1.0, trend ** ((week - self.start).days / 7.0)):
                        continue
                    if rng.random() < 0.22 * (_season(week, eid, lull) - 1.0):
                        quantity += 1  # holiday baskets are bigger, not just more numerous
                    demand[sku][week] += quantity
                    if self._out_of_stock(sku, week):
                        continue  # wanted it, could not have it
                    online, store, mall = CHANNEL_MIX[sku]
                    if week in activations:
                        # A stand in a mall over an Eid weekend does real volume
                        # while it is standing — that is the whole reason to pay
                        # for the floor space.
                        mall *= 4.0
                    else:
                        # No stand this week. That trade does not vanish, it
                        # happens at the counter instead.
                        store, mall = store + mall, 0.0
                    where = rng.choices(
                        ["online", "store", "mall"], weights=[online, store, mall], k=1
                    )[0]
                    day = rng.choices(range(7), weights=WEEKDAY_WEIGHT, k=1)[0]
                    when = datetime.combine(
                        week + timedelta(days=day),
                        time(hour=rng.randint(10, 22), minute=rng.choice([0, 15, 30, 45])),
                        tzinfo=UTC,
                    )
                    order_id += 1
                    plan[person].append(
                        {
                            "order_id": order_id,
                            "sku": sku,
                            "brand": brand,
                            "price": str(paid),
                            "list_price": price,
                            "discount": markdown,
                            "variant": variant,
                            "quantity": quantity,
                            "when": when,
                            "where": where,
                            # Whether she gave a name at the counter. Decided
                            # here rather than at send time so a re-run makes the
                            # same sale anonymous as the first run did.
                            "named": where == "online" or rng.random() < CAPTURE[where],
                        }
                    )
            plan[person].sort(key=lambda o: o["when"])
        return plan, demand

    def _stock_readings(self, demand: dict[str, dict[date, int]]) -> list[dict]:
        """A believable inventory ledger behind that demand.

        Bought in batches against a cover target, arriving after a lead time, and
        going to zero across the planned outages. Posted weekly so the forecast
        can measure demand over the days an item was actually sellable — without
        this every week looks equally available and a sold-out fortnight arrives
        as a fortnight of no demand.
        """
        readings: list[dict] = []
        weeks = self._weeks()
        for sku, by_week in demand.items():
            average = max(1.0, sum(by_week.values()) / max(1, len(weeks)))
            on_hand = int(average * 6)
            incoming: dict[date, int] = {}
            on_order = 0
            for week in weeks:
                arrived = incoming.pop(week, 0)
                on_hand += arrived
                on_order -= arrived
                if self._out_of_stock(sku, week):
                    on_hand = 0
                readings.append(
                    {
                        "sku": sku,
                        "on_hand": on_hand,
                        "on_order": max(0, on_order),
                        "recorded_at": datetime.combine(week, time(hour=6), tzinfo=UTC),
                    }
                )
                on_hand = max(0, on_hand - by_week[week])
                # Reorder at three weeks of cover, up to eight, arriving in three.
                if on_hand + on_order < average * 3 and not any(
                    self._out_of_stock(sku, w) for w in (week, week + timedelta(weeks=1))
                ):
                    quantity = int(average * 8) - on_hand - on_order
                    if quantity > 0:
                        incoming[week + timedelta(weeks=3)] = quantity
                        on_order += quantity
        return readings

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
                    # Shopify's own field names. The connector keeps the whole
                    # payload, so a size or a shade recorded here is on the
                    # event and available to anything that later wants to
                    # forecast the mix rather than only the line.
                    "variant_title": order["variant"],
                    "variant_id": f"{order['sku']}::{order['variant']}",
                    "compare_at_price": order["list_price"],
                }
            ],
            "total_discounts": str(
                (Decimal(order["list_price"]) - Decimal(order["price"]))
                * order["quantity"]
            ),
        }

    def _sale(self, person: Customer, order: dict) -> dict:
        """One basket rung up off the storefront.

        ``move_stock`` is off: the weekly ledger this script posts already states
        the position at the top of each week, and taking the same units off twice
        would invent stockouts nobody had. ``receipt`` carries the order id so a
        second run collapses onto the first rather than ringing the sale again —
        the dedupe key is source, till and receipt.
        """
        store = order["where"] == "store"
        return {
            "lines": [
                {
                    "sku": order["sku"],
                    "quantity": order["quantity"],
                    "unit_price": order["price"],
                    "variant": order["variant"],
                }
            ],
            # Nothing at all when she did not give it. A name with no phone or
            # email cannot be resolved to a person and would only make the
            # walk-in record look identified.
            "phone": person.phone if order["named"] else None,
            "email": person.email if order["named"] else None,
            "name": person.name if order["named"] else None,
            "till": "counter" if store else "mall-stand",
            "currency": "SAR",
            # The shop's own till, not Shopify's. "shopify_pos" is a Shopify
            # order rung up on a Shopify terminal, which these are not — the
            # counters here are independent of the storefront entirely, and
            # filing them under Shopify's name made the group look like it ran
            # its shops on Shopify POS.
            "source": "pos" if store else "activation",
            "channel": "retail" if store else "event",
            "move_stock": False,
            "receipt": f"seed-{order['order_id']}",
            "occurred_at": order["when"].isoformat(),
        }

    async def _post(self, client: httpx.AsyncClient, url: str, **kwargs) -> httpx.Response:
        """Retry the transient failures a long run over a remote database hits.

        A managed Postgres a continent away will drop or stall a connection
        somewhere in several thousand requests, and the request that lands on it
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
        plan, demand = self._plan()
        weeks = self._weeks()
        placed = sum(len(v) for v in plan.values())
        units: dict[str, int] = {}
        for orders in plan.values():
            for order in orders:
                units[order["sku"]] = units.get(order["sku"], 0) + order["quantity"]
        where: dict[str, int] = {}
        named = 0
        for orders in plan.values():
            for order in orders:
                where[order["where"]] = where.get(order["where"], 0) + 1
                named += bool(order["named"])
        print(
            f"{len(plan)} customers, {placed} orders, {len(weeks)} weeks "
            f"({self.start} to {self.end})"
        )
        print(
            "  " + ", ".join(f"{k} {v}" for k, v in sorted(where.items()))
            + f" · {named}/{placed} carry a customer"
        )

        limiter = asyncio.Semaphore(self.concurrency)
        done = 0

        async with httpx.AsyncClient(base_url=self.base_url, timeout=60) as client:
            client.headers["X-API-Key"] = self.api_key

            if self.with_stock:
                # The forecast it already published, echoed back. The stock
                # endpoint takes the whole position at once, and defaulting the
                # forecast to zero here would quietly unpublish it.
                current = {}
                try:
                    for item in (await client.get("/items")).json():
                        current[item["sku"]] = item.get("weekly_forecast") or "0"
                except (httpx.HTTPError, ValueError):
                    pass
                readings = self._stock_readings(demand)
                print(f"posting {len(readings)} stock readings")
                for reading in readings:
                    await self._post(
                        client, "/stock",
                        json={
                            **reading,
                            "recorded_at": reading["recorded_at"].isoformat(),
                            "weekly_forecast": current.get(reading["sku"], "0"),
                        },
                    )

            async def place(person: Customer, orders: list[dict]) -> None:
                nonlocal done
                # One customer's orders go in sequence. Two requests resolving the
                # same identity at once race on the person row, and the loser is a
                # duplicate profile — the exact failure a CDP exists to prevent.
                async with limiter:
                    person_id = None
                    for order in orders:
                        if order["where"] == "online":
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
                            person_id = response.json().get("person_id") or person_id
                        else:
                            # The counter and the mall stand write through the
                            # same endpoint the console's own capture form uses,
                            # so this exercises the path a shop assistant takes
                            # rather than a private one built for the seed.
                            sale = await self._post(
                                client, "/sales", json=self._sale(person, order)
                            )
                            # Only when she gave a name. An anonymous basket
                            # resolves to the till's standing walk-in record, and
                            # granting that record consent would put every future
                            # walk-in inside a marketing audience.
                            if order["named"]:
                                person_id = sale.json().get("person_id") or person_id
                        done += 1
                        if done % 250 == 0:
                            print(f"  {done}/{placed}")
                    # Consent for most, not all: the gate has to be visible doing
                    # something or it reads as decoration.
                    # Keyed on the stable id, not on hash(): PYTHONHASHSEED is
                    # randomised per process, so a rerun would silently grant
                    # consent to a different set of people than the first run.
                    if person_id and person.consents:
                        # One grant per brand she bought from — the box belongs
                        # to that brand's till, and a grant made at Rawash's
                        # counter is not Aleena's to rely on.
                        bought = {o["brand"].lower() for o in orders}
                        online = {
                            o["brand"].lower() for o in orders if o["where"] == "online"
                        }
                        for brand in sorted(bought):
                            at_checkout = brand in online
                            for purpose in person.consents:
                                await self._post(
                                    client,
                                    f"/persons/{person_id}/consent",
                                    json={
                                        "purpose": purpose,
                                        "granted": True,
                                        "brand": brand,
                                        # Where she actually ticked it. Two of the
                                        # three brands have no storefront, so
                                        # filing their grants as checkout opt-ins
                                        # would put a signature on a page that
                                        # does not exist.
                                        "source": (
                                            "shopify_checkout" if at_checkout
                                            else "in_store"
                                        ),
                                        "evidence": (
                                            "checkout opt-in checkbox" if at_checkout
                                            else "signed slip at the till"
                                        ),
                                    },
                                )

            await asyncio.gather(*(place(p, o) for p, o in plan.items() if o))

        print("\nunits sold, and what that is per week:")
        for sku, count in sorted(units.items(), key=lambda kv: -kv[1]):
            print(f"  {sku:<14} {count:>6} units   {count / len(weeks):>7.1f}/wk")

        # The shape, said out loud. If these four lines are flat, the seed has
        # not done its job and no forecast built on it will beat an average.
        print("\nquarterly units, to show the season is actually there:")
        for sku in units:
            by_quarter: dict[str, int] = {}
            for week, count in demand[sku].items():
                key = f"{week.year}Q{(week.month - 1) // 3 + 1}"
                by_quarter[key] = by_quarter.get(key, 0) + count
            trail = "  ".join(f"{k} {v:>5}" for k, v in sorted(by_quarter.items()))
            print(f"  {sku:<14} {trail}")



def main() -> None:
    from cdp.config import get_settings as cdp_settings
    from sca.config import get_settings as platform_settings

    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8101")
    parser.add_argument("--api-key", default=platform_settings().api_key)
    parser.add_argument(
        "--shopify-secret", default=cdp_settings().shopify_webhook_secret or "dev-shopify-secret"
    )
    parser.add_argument("--customers", type=int, default=180)
    # Absolute, and a Monday. Two years and a bit, so both Eids and both summers
    # sit inside the window and each has happened twice by the time the model is
    # asked to recognise them.
    parser.add_argument("--start", default="2024-08-05")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument(
        "--no-stock", action="store_true",
        help="skip the weekly stock ledger (leaves the forecast unable to tell a "
             "sold-out week from a quiet one)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="print the shape of the history and send nothing",
    )
    args = parser.parse_args()

    start = _monday(date.fromisoformat(args.start))
    # Complete weeks only. A part-week at the end reads as a collapse in demand.
    end = _monday(datetime.now(UTC).date())

    seeder = Seeder(
        args.base_url,
        args.api_key,
        args.shopify_secret,
        args.customers,
        start,
        end,
        args.concurrency,
        not args.no_stock,
    )
    if args.dry_run:
        plan, demand = seeder._plan()
        weeks = seeder._weeks()
        placed = sum(len(v) for v in plan.values())
        print(f"{len(plan)} customers, {placed} orders, {len(weeks)} weeks ({start} to {end})")
        print("\nquarterly units:")
        for sku, by_week in demand.items():
            by_quarter: dict[str, int] = {}
            for week, count in by_week.items():
                key = f"{week.year}Q{(week.month - 1) // 3 + 1}"
                by_quarter[key] = by_quarter.get(key, 0) + count
            print(f"  {sku:<14} " + "  ".join(f"{k} {v:>5}" for k, v in sorted(by_quarter.items())))
        return
    asyncio.run(seeder.run())


if __name__ == "__main__":
    main()
