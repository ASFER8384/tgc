"""Put the history on a shelf, and the stock where the history says it sits.

Locations arrived after the trading history did. Every seeded sale was recorded
before there was anywhere to record it *from*, and the migration placed all
existing stock at the storefront because that is the one shelf whose count can be
checked in a second. For a group that sells almost entirely over a counter, both
of those are wrong in the same direction: the shops appear to hold nothing and to
have sold nothing.

Two passes, and the second depends on the first.

**Where each sale happened.** A customer is given a home shop and keeps it. People
shop near where they live, so scattering one woman's orders across two cities
would invent a travelling customer and destroy the per-shop demand it is supposed
to be establishing. The mall stand is run out of Riyadh, so its sales land there.

This is an assignment, not a recovery — nobody recorded which shop, and no amount
of care changes that. Each event is stamped ``location_backfilled`` so the guess
is never mistaken later for something that was observed.

**Where the stock sits.** Split in proportion to what each shelf actually sold.
A shop that took a third of the units needs roughly a third of the cover, and
that is a ratio derived from the history rather than a number somebody imagined.
Totals are preserved exactly: this moves stock between shelves and creates none.

    .venv/Scripts/python -m scripts.backfill_locations            # says what it would do
    .venv/Scripts/python -m scripts.backfill_locations --apply
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from hashlib import blake2b

sys.path.insert(0, "src")

from sqlalchemy import select  # noqa: E402

from cdp.models import Event  # noqa: E402
from sca.db import get_sessionmaker  # noqa: E402
from sca.models import StockAtLocation, StockSnapshot  # noqa: E402

# Which shelf a source sold from. The storefront is its own; the counter is
# whichever shop the customer belongs to; the mall stand is run out of Riyadh
# and its takings belong to that shop's books.
ONLINE_SOURCES = {"shopify"}
MALL_SOURCES = {"activation"}
SHOPS = ("riyadh", "jeddah")

# How the customer base splits between the two shops. Riyadh is the larger of
# them, which is the only reason it is not an even cut.
RIYADH_SHARE = 0.58


def home_shop(person_id: str) -> str:
    """The shop a customer belongs to, stable for the life of the record.

    Hashed rather than random: this script has to be able to run twice and reach
    the same answer, or a second pass would move half the customer base to the
    other city and rewrite the demand history behind every per-shop figure.
    """
    digest = blake2b(person_id.encode(), digest_size=8).digest()
    position = int.from_bytes(digest, "big") / float(1 << 64)
    return SHOPS[0] if position < RIYADH_SHARE else SHOPS[1]


def location_for(source: str, person_id: str) -> str:
    if source in ONLINE_SOURCES:
        return "online"
    if source in MALL_SOURCES:
        return SHOPS[0]
    return home_shop(person_id)


async def main(apply: bool, online_percent: float | None, history_only: bool = False) -> None:
    """``online_percent`` overrides what history says the storefront should hold.

    History cannot answer this one. The seeded trade is almost entirely counter
    and mall, so demand alone puts nearly nothing on the storefront's shelf —
    which would be right for a business that had never sold online and is wrong
    for one that has just stocked its storefront on purpose. Somebody has made a
    decision Shopify already knows about, and a script reading two years of the
    past is not in a position to overrule it.
    """
    async with get_sessionmaker()() as session:
        rows = (
            await session.scalars(select(Event).where(Event.name == "order_paid"))
        ).all()

        placed: Counter[str] = Counter()
        units: Counter[str] = Counter()
        per_sku: dict[str, Counter[str]] = {}
        touched = 0

        for event in rows:
            payload = dict(event.payload or {})
            where = location_for(event.source, event.person_id)
            placed[where] += 1

            for candidate in (payload, payload.get("order") or {}):
                lines = candidate.get("line_items")
                if isinstance(lines, list):
                    break
            else:
                lines = []
            for line in lines:
                sku = (line.get("sku") or "").strip()
                if not sku:
                    continue
                try:
                    quantity = int(line.get("quantity") or 0)
                except (TypeError, ValueError):
                    continue
                if quantity > 0:
                    units[where] += quantity
                    per_sku.setdefault(sku, Counter())[where] += quantity

            if payload.get("location") != where:
                payload["location"] = where
                # Stamped so the guess is never later mistaken for a record. No
                # shop was written down at the time and this cannot recover one.
                payload["location_backfilled"] = True
                event.payload = payload
                touched += 1

        print(f"{len(rows)} order(s) read, {touched} to stamp")
        print("  orders by shelf:", dict(sorted(placed.items())))
        print("  units by shelf :", dict(sorted(units.items())))

        # Two jobs, and a reason to want only the first. Stamping history is
        # safe and repeatable; moving stock overwrites what is on the shelves
        # now, and on a database where somebody has since counted a rail that is
        # the shop's own work being thrown away. The flag existed and was
        # ignored, so asking for the safe half quietly did both.
        if history_only:
            if not apply:
                print("\ndry run — nothing written. Re-run with --apply.")
                return
            await session.commit()
            print(f"\napplied to history only. {touched} order(s) stamped, stock untouched.")
            return

        # Stock follows the demand it serves, per item — a shop that sells the
        # lipstick and not the abayas should not be given abayas because it is
        # busy overall.
        print("\nstock, moved to match:")
        moves: list[tuple] = []
        for snapshot in await session.scalars(select(StockSnapshot)):
            sold = per_sku.get(snapshot.sku)
            total = snapshot.on_hand
            if not sold or total <= 0:
                continue
            everywhere = sum(sold.values())
            if online_percent is None:
                split = {
                    where: total * count // everywhere for where, count in sold.items()
                }
            else:
                # The storefront takes what it was told to take; the shops divide
                # the rest between them in the proportion they actually sell.
                reserved = int(total * online_percent)
                shop_sales = {k: v for k, v in sold.items() if k in SHOPS}
                shop_total = sum(shop_sales.values()) or 1
                remainder = total - reserved
                split = {
                    where: remainder * count // shop_total
                    for where, count in shop_sales.items()
                }
                split["online"] = reserved
            # Whatever integer division dropped goes to the busiest shelf rather
            # than being lost: the totals have to match exactly or the buying
            # desk and the shelves disagree about what the group holds.
            short = total - sum(split.values())
            if short:
                busiest = max(
                    (k for k in split if k in SHOPS),
                    key=lambda k: sold.get(k, 0),
                    default="online",
                )
                split[busiest] += short
            moves.append((snapshot.sku, total, split))
            print(f"  {snapshot.sku:<14} {total:>5} -> {dict(sorted(split.items()))}")

        if not apply:
            print("\ndry run — nothing written. Re-run with --apply.")
            return

        for sku, _total, split in moves:
            for row in await session.scalars(
                select(StockAtLocation).where(StockAtLocation.sku == sku)
            ):
                row.on_hand = 0
            for where, quantity in split.items():
                row = await session.get(StockAtLocation, (sku, where))
                if row is None:
                    row = StockAtLocation(sku=sku, location_code=where)
                    session.add(row)
                row.on_hand = quantity
        await session.commit()
        print("\napplied.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--history-only", action="store_true",
        help="stamp the orders and leave stock alone",
    )
    parser.add_argument(
        "--online-percent", type=float, default=None,
        help="share of each item's stock to hold on the storefront, 0-100. "
             "Omitted, the split follows what each shelf actually sold — which "
             "puts almost nothing online, because the history is counter trade.",
    )
    args = parser.parse_args()
    share = None if args.online_percent is None else max(0.0, min(args.online_percent, 100.0)) / 100
    asyncio.run(main(args.apply, share, args.history_only))
