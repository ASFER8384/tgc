"""Re-file the trading history under the brand each basket was actually from.

Every order carries a ``brands`` split — how much of that basket belonged to
which brand — and it is computed from ``Item.brand`` at the moment the sale is
written. The catalogue said "aleena" for all four items, including the Rawash
lipstick and the Aynola gift box, so every sale in the seeded history was filed
one hundred per cent to Aleena whatever was in it.

That split is what each customer's per-brand spend is built from. So "top Rawash
customers" held four people, Aleena's revenue was overstated by the whole of the
other two brands, and any audience cut by brand would have gone to the wrong
list — which is the part that matters, because a send is not recallable.

Nothing here is guessed. Every line carries its own SKU and its own price, and
the catalogue now names the brand correctly, so this is arithmetic on data that
was already in the row. The rule is exactly the one ``/sales`` applies when it
writes a new sale: line price times quantity, per brand, and a line with no
price contributes demand but no money.

**Shopify's own orders are left alone.** Their split comes from the vendor on
each line item, which was right all along — the storefront knew the lipstick was
Rawash even while this catalogue did not.

    .venv/Scripts/python -m scripts.rebrand_history            # says what would change
    .venv/Scripts/python -m scripts.rebrand_history --apply
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from decimal import Decimal

sys.path.insert(0, "src")

from sqlalchemy import select  # noqa: E402

from cdp.models import Event  # noqa: E402
from cdp.profiles.service import ProfileService  # noqa: E402
from sca.db import get_sessionmaker  # noqa: E402
from sca.models import Item  # noqa: E402

# Sources whose split is derived from Shopify's own vendor field rather than
# from this catalogue. They were never wrong and must not be rewritten — the
# vendor is the authority on brand, and overwriting it from here would replace a
# fact with a lookup.
VENDOR_SOURCED = {"shopify", "shopify_pos"}

UNASSIGNED = "unassigned"


def split_for(lines: list[dict], brand_by_sku: dict[str, str | None]) -> dict[str, Decimal] | None:
    """The brand split this basket should have, or None if it cannot be recomputed.

    None where any line names a SKU the catalogue does not hold. A partial
    recompute would move some of the basket's money to the right brand and strand
    the rest, which is a worse record than the wrong one it replaced — at least
    the wrong one adds up.
    """
    out: dict[str, Decimal] = {}
    for line in lines:
        sku = (line.get("sku") or "").strip()
        if not sku or sku not in brand_by_sku:
            return None
        price = line.get("price")
        if price in (None, ""):
            # Demand without money, exactly as /sales records it. The units are
            # still in the line items and still reach the forecast.
            continue
        try:
            total = Decimal(str(price)) * int(line.get("quantity") or 0)
        except (ArithmeticError, TypeError, ValueError):
            return None
        brand = (brand_by_sku[sku] or UNASSIGNED).lower()
        out[brand] = out.get(brand, Decimal("0")) + total
    return {k: v for k, v in out.items() if v > 0}


def as_stored(split: dict[str, Decimal]) -> dict[str, str]:
    return {k: f"{v:.2f}" for k, v in sorted(split.items())}


async def main(apply: bool) -> None:
    async with get_sessionmaker()() as session:
        brand_by_sku = {
            i.sku: i.brand for i in await session.scalars(select(Item))
        }
        print(f"catalogue: {brand_by_sku}\n")

        rows = (
            await session.scalars(select(Event).where(Event.name == "order_paid"))
        ).all()

        before: Counter[str] = Counter()
        after: Counter[str] = Counter()
        changed: list[Event] = []
        skipped_vendor = 0
        skipped_unknown = 0

        for event in rows:
            payload = dict(event.payload or {})
            current = {k: Decimal(str(v)) for k, v in (payload.get("brands") or {}).items()}
            for brand, amount in current.items():
                before[brand] += amount

            if event.source in VENDOR_SOURCED:
                skipped_vendor += 1
                for brand, amount in current.items():
                    after[brand] += amount
                continue

            lines = payload.get("line_items")
            if not isinstance(lines, list):
                skipped_unknown += 1
                for brand, amount in current.items():
                    after[brand] += amount
                continue

            wanted = split_for(lines, brand_by_sku)
            if wanted is None:
                skipped_unknown += 1
                for brand, amount in current.items():
                    after[brand] += amount
                continue

            for brand, amount in wanted.items():
                after[brand] += amount

            if as_stored(wanted) != {k: f"{v:.2f}" for k, v in sorted(current.items())}:
                payload["brands"] = as_stored(wanted)
                # The per-line brand is stored too and was wrong in the same way.
                # Left behind, it would be the copy somebody reads when they open
                # one order to check the total they did not believe.
                payload["line_items"] = [
                    {**line, "brand": brand_by_sku.get((line.get("sku") or "").strip())}
                    if (line.get("sku") or "").strip() in brand_by_sku else line
                    for line in lines
                ]
                event.payload = payload
                changed.append(event)

        print(f"{len(rows)} paid order(s)")
        print(f"  {len(changed)} to re-file")
        print(f"  {skipped_vendor} left alone — Shopify's own, split by vendor")
        print(f"  {skipped_unknown} left alone — a line this catalogue cannot resolve")
        print("\nrevenue by brand:")
        for brand in sorted(set(before) | set(after)):
            was, now = before.get(brand, Decimal(0)), after.get(brand, Decimal(0))
            mark = "" if was == now else "   <-- moves"
            print(f"  {brand:<12} {was:>12,.2f} -> {now:>12,.2f}{mark}")

        people = sorted({e.person_id for e in changed if e.person_id})
        print(f"\n{len(people)} customer profile(s) would be rebuilt")

        if not apply:
            print("\ndry run — nothing written. Re-run with --apply.")
            return

        await session.flush()
        # The stats are derived, so they are rebuilt from the corrected events
        # rather than edited. Nothing is added to a total here; each profile is
        # recomputed from its own timeline, which is the only way the numbers can
        # be trusted afterwards.
        profiles = ProfileService(session)
        for n, person_id in enumerate(people, 1):
            await profiles.recompute(person_id)
            if n % 200 == 0:
                print(f"  rebuilt {n}/{len(people)}")
        await session.commit()
        print(f"\napplied — {len(changed)} order(s) re-filed, {len(people)} profile(s) rebuilt.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    asyncio.run(main(parser.parse_args().apply))
