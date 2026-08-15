"""Put a size on the in-house sales that never recorded one.

A Shopify line says what was bought: "Classic Red", "52". A counter line says
"RWS-LIP-TUBE" and stops, so a customer's history reads *she bought a lipstick*
where the website's reads *she bought the Rose Nude*. Since the counter is most
of the trade, the size curve was being measured from the minority of sales that
happen to be online.

The weighting is the size mix of sales that **do** name one, per SKU, taken
across every channel. That is a demand curve rather than a stock curve, which
matters: the shelf mix says what was bought *for*, and this is trying to say
what was bought. Where a SKU has no such sales at all it falls back to the
storefront's stock mix, and where it has neither it is left alone.

Deterministic, from the event id and the line's position. It has to be, or a
second run would reshuffle every size in the history and every figure derived
from it would move for no reason anybody could name.

**An assignment, not a recovery.** Nobody wrote down which shade she bought and
nothing can recover it. Each line is stamped ``variant_backfilled`` so the guess
is never later mistaken for something observed, exactly as the shop locations
were.

One size per line, not a mix: a line item names one variant, which is how
Shopify models a basket and how the till will record one.

    .venv/Scripts/python -m scripts.backfill_sale_variants            # says what it would do
    .venv/Scripts/python -m scripts.backfill_sale_variants --apply
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
from sca.models import ShopifyVariant  # noqa: E402

# Sources whose lines come from Shopify. A line of theirs that names a size is
# left alone — the storefront recorded what was actually bought, and a guess must
# never overwrite an observation.
#
# A line of theirs that names none is a different case, and the distinction took
# a lookup to establish rather than an assumption: their variant ids no longer
# resolve against the mirrored catalogue, because those products were deleted and
# rebuilt in the store. So there is no size to preserve and none to recover, and
# filling one in is not overwriting Shopify's answer — Shopify gave none.
#
# Lines with no SKU stay untouched whatever their source. They belong to no item
# here, and a size against an item this platform does not stock is a label on
# nothing.
VENDOR_SOURCED = {"shopify", "shopify_pos"}


def pick(weights: dict[str, int], seed: str) -> str:
    """One size, chosen by weight, the same way every time for the same seed."""
    sizes = sorted(weights)
    pool = sum(weights[s] for s in sizes)
    digest = blake2b(seed.encode(), digest_size=8).digest()
    position = int.from_bytes(digest, "big") / float(1 << 64) * pool
    running = 0.0
    for size in sizes:
        running += weights[size]
        if position < running:
            return size
    return sizes[-1]


async def main(apply: bool) -> None:
    async with get_sessionmaker()() as session:
        rows = (
            await session.scalars(select(Event).where(Event.name == "order_paid"))
        ).all()

        # What sizes actually sell, per SKU, from every line that names one.
        sold: dict[str, Counter[str]] = {}
        for event in rows:
            for line in (event.payload or {}).get("line_items") or []:
                title = (line.get("variant_title") or "").strip()
                sku = (line.get("sku") or "").strip()
                if title and sku and not line.get("variant_backfilled"):
                    try:
                        units = int(line.get("quantity") or 0)
                    except (TypeError, ValueError):
                        units = 0
                    if units > 0:
                        sold.setdefault(sku, Counter())[title] += units

        # The storefront's stock mix, for anything with no sold history at all.
        shelf: dict[str, Counter[str]] = {}
        for row in await session.scalars(select(ShopifyVariant)):
            title = (row.variant_title or "").strip()
            if row.sku and title and row.tracked and row.on_hand > 0:
                shelf.setdefault(row.sku, Counter())[title] += row.on_hand

        print("size mix per item, from sales that name one:")
        for sku in sorted(sold):
            print(f"  {sku:<14} {dict(sold[sku].most_common())}")
        missing = sorted(set(shelf) - set(sold))
        if missing:
            print(f"  falling back to the storefront's stock mix for: {', '.join(missing)}")
        print()

        # The catalogue's brand per SKU. Shopify lines carry a vendor and no
        # per-line brand, so the same order was labelled at the order level and
        # blank on every line inside it. Filled from the SKU rather than the
        # vendor: the catalogue is what the rest of the platform reads, and two
        # routes to one label is how they drift.
        brand_by_sku = {}
        from sca.models import Item as _Item
        for row in await session.scalars(select(_Item)):
            brand_by_sku[row.sku] = row.brand

        stamped, lines_done, brands_done = 0, 0, 0
        no_evidence: set[str] = set()
        assigned: dict[str, Counter[str]] = {}

        for event in rows:
            payload = dict(event.payload or {})
            items = payload.get("line_items")
            if not isinstance(items, list):
                continue
            touched = False
            out = []
            for index, line in enumerate(items):
                line = dict(line)
                sku = (line.get("sku") or "").strip()
                # The brand, wherever it is missing and the SKU is known. Not a
                # guess: the catalogue is the record of which brand a SKU is.
                if sku and sku in brand_by_sku and not line.get("brand"):
                    line["brand"] = brand_by_sku[sku]
                    brands_done += 1
                    touched = True
                if sku and sku in brand_by_sku and not (line.get("variant_title") or "").strip():
                    weights = sold.get(sku) or shelf.get(sku)
                    if weights:
                        size = pick(dict(weights), f"{event.id}:{sku}:{index}")
                        line["variant_title"] = size
                        # Stamped so the guess is never later read as a record.
                        # Nobody wrote down which shade she bought.
                        line["variant_backfilled"] = True
                        assigned.setdefault(sku, Counter())[size] += 1
                        touched = True
                        lines_done += 1
                    else:
                        no_evidence.add(sku)
                out.append(line)
            if touched:
                payload["line_items"] = out
                event.payload = payload
                stamped += 1

        print(f"{len(rows)} paid order(s)")
        print(f"  {stamped} order(s) to stamp, {lines_done} line(s) given a size, "
              f"{brands_done} line(s) given a brand")
        if no_evidence:
            print(f"  no size evidence for: {', '.join(sorted(no_evidence))} — left alone")
        print("\nwhat would be assigned:")
        for sku in sorted(assigned):
            print(f"  {sku:<14} {dict(assigned[sku].most_common())}")

        if not apply:
            print("\ndry run — nothing written. Re-run with --apply.")
            return

        await session.commit()
        print(f"\napplied — {lines_done} line(s) on {stamped} order(s).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    asyncio.run(main(parser.parse_args().apply))
