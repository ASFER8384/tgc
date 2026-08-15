"""Give each shop's rail a size breakdown, in the shape the storefront's is.

The shops hold their stock as one number per item. The storefront holds its by
size. So a rail of one abaya in Jeddah cannot say whether that one is a 52 or a
60, and the size boxes on the stock table sit empty with a total beside them
that nobody can reconcile against them.

This fills them in. Every shop shelf that holds something and has never been
broken down is split across the sizes the storefront actually sells, in the
proportion the storefront holds them — which is the only evidence available
about the size mix, and better than an even cut that would claim the shops stock
sizes uniformly when the website plainly does not.

**This is an assignment, not a count.** Nobody walked these rails. The totals are
preserved exactly, so nothing downstream moves and the buying desk sees the same
figures it saw before — but the number against any one size is a plausible
guess, and the first person to walk the rail should overwrite it rather than
trust it. That is the whole point of the count boxes.

Shelves that already have sizes on them are left alone: somebody has counted
those, and a guess must never overwrite an observation.

    .venv/Scripts/python -m scripts.backfill_variant_stock            # says what it would do
    .venv/Scripts/python -m scripts.backfill_variant_stock --apply
"""

from __future__ import annotations

import argparse
import asyncio
import sys

sys.path.insert(0, "src")

from sqlalchemy import select  # noqa: E402

from sca.db import get_sessionmaker  # noqa: E402
from sca.models import (  # noqa: E402
    Item,
    ShopifyVariant,
    StockAtLocation,
    StockAtVariant,
    StockLocation,
)

# The storefront is not a shop and is not touched. Its breakdown is Shopify's
# own, read on every pull, and writing a guess over it would be replacing a fact
# with an estimate.
ONLINE = "online"


def spread(total: int, weights: dict[str, int]) -> dict[str, int]:
    """Split ``total`` across sizes in proportion to ``weights``, exactly.

    Largest remainder, because the obvious floor-and-move-on loses units: five
    sizes each rounding down turns a rail of three into a rail of nothing, and a
    breakdown that does not add up to the shelf is worse than no breakdown.
    """
    if total <= 0 or not weights:
        return {}
    pool = sum(weights.values())
    if pool <= 0:
        # The storefront holds none of it either, so it has no opinion on the
        # mix. An even cut is the honest fallback — it claims nothing.
        weights = dict.fromkeys(weights, 1)
        pool = len(weights)

    exact = {k: total * v / pool for k, v in weights.items()}
    out = {k: int(v) for k, v in exact.items()}
    short = total - sum(out.values())
    # Whatever rounding dropped goes to the sizes with most left over, biggest
    # first, so the result is the closest whole split rather than a tidy one.
    for size in sorted(exact, key=lambda k: (exact[k] - out[k], weights[k]), reverse=True):
        if short <= 0:
            break
        out[size] += 1
        short -= 1
    return {k: v for k, v in out.items() if v > 0}


async def main(apply: bool) -> None:
    async with get_sessionmaker()() as session:
        shops = {
            p.code: p.name
            for p in await session.scalars(select(StockLocation))
            if p.active and p.kind != ONLINE
        }
        items = {i.sku: i for i in await session.scalars(select(Item))}

        # The size mix the storefront holds, per item. This is the weighting.
        mix: dict[str, dict[str, int]] = {}
        for row in await session.scalars(select(ShopifyVariant)):
            if not row.sku or not row.tracked:
                continue
            key = row.variant_title or ""
            if key:
                mix.setdefault(row.sku, {})[key] = mix.get(row.sku, {}).get(key, 0) + max(
                    0, row.on_hand
                )

        already: set[tuple[str, str]] = set()
        for row in await session.scalars(select(StockAtVariant)):
            already.add((row.sku, row.location_code))

        planned: list[tuple[str, str, dict[str, int]]] = []
        skipped_counted = 0
        skipped_nosizes: set[str] = set()

        for shelf in await session.scalars(select(StockAtLocation)):
            if shelf.location_code not in shops or shelf.on_hand <= 0:
                continue
            if (shelf.sku, shelf.location_code) in already:
                skipped_counted += 1
                continue
            weights = dict(mix.get(shelf.sku) or {})
            if not weights:
                # No storefront product, so fall back to the item's own list. An
                # item with neither is counted as one number and stays that way.
                own = list((items.get(shelf.sku) and items[shelf.sku].variants) or [])
                weights = dict.fromkeys(own, 1)
            if not weights:
                skipped_nosizes.add(shelf.sku)
                continue
            split = spread(shelf.on_hand, weights)
            if split:
                planned.append((shelf.sku, shelf.location_code, split))

        print(f"{len(planned)} shelf/shelves to break down")
        if skipped_counted:
            print(f"  {skipped_counted} left alone — already counted by size")
        if skipped_nosizes:
            print(f"  {len(skipped_nosizes)} item(s) have no sizes named: "
                  f"{', '.join(sorted(skipped_nosizes))}")
        print()
        for sku, code, split in sorted(planned):
            total = sum(split.values())
            print(f"  {sku:<14} {shops[code]:<12} {total:>4} -> "
                  f"{ {k: split[k] for k in sorted(split)} }")

        if not apply:
            print("\ndry run — nothing written. Re-run with --apply.")
            return

        for sku, code, split in planned:
            for size, units in split.items():
                session.add(
                    StockAtVariant(
                        sku=sku, location_code=code, variant=size, on_hand=units
                    )
                )
        await session.commit()
        # No re-add afterwards, deliberately: each split sums to the shelf it came
        # from, so every total above is already right. Calling the refold would
        # write a ledger reading for a movement that did not happen.
        print(f"\napplied — {len(planned)} shelf/shelves broken down. Totals unchanged.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    asyncio.run(main(parser.parse_args().apply))
