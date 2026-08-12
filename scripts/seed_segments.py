"""Replace the hand-made demo audiences with the standard RFM set.

Champions, Loyal, At Risk and the rest are the vocabulary every retail marketer
already has, so they need no explaining in a demo. They are defined here from
traits the platform actually computes — order count and days since the last
order — rather than from an RFM string, because a definition somebody can read
is one they can argue with.

One set per brand, not one set overall. Consent is granted per brand, so
"Champions who may be messaged" is a different group of people for Aleena than
for Rawash, and collapsing them would produce exactly the company-wide audience
nobody agreed to.

    python scripts/seed_segments.py
"""

import asyncio

from sqlalchemy import delete, select

from cdp.models import BRANDS, ActivationDelivery, ActivationRun, Segment, SegmentMember
from sca.db import get_sessionmaker

# label, key suffix, definition. Ordered as the console shows them: best first,
# then the ones that need attention.
AUDIENCES = [
    ("Champions", "champions",
     {"all": [{"trait": "order_count", "op": "gte", "value": 4},
              {"trait": "recency_days", "op": "lte", "value": 60}]}),
    ("Loyal Customers", "loyal",
     {"all": [{"trait": "order_count", "op": "gte", "value": 3}]}),
    ("Potential Loyalists", "potential",
     {"all": [{"trait": "order_count", "op": "gte", "value": 2},
              {"trait": "recency_days", "op": "lte", "value": 90}]}),
    ("New Customers", "new",
     {"all": [{"trait": "order_count", "op": "lte", "value": 1},
              {"trait": "recency_days", "op": "lte", "value": 30}]}),
    ("At Risk", "at_risk",
     {"all": [{"trait": "order_count", "op": "gte", "value": 2},
              {"trait": "recency_days", "op": "gt", "value": 90}]}),
    ("Lost", "lost",
     {"all": [{"trait": "recency_days", "op": "gt", "value": 180}]}),
    ("All Customers", "all",
     {"all": [{"trait": "order_count", "op": "gte", "value": 0}]}),
]


async def main() -> None:
    async with get_sessionmaker()() as session:
        # The old audiences go, and everything pointing at them goes first.
        # Deliberately loud about the delivery log: those rows are the record of
        # who was messaged and on what basis, and dropping them silently would
        # be exactly the kind of quiet history loss this platform exists to
        # prevent. Retiring an audience is the one time it is the right call.
        keys = list(await session.scalars(select(Segment.key)))
        wanted = {f"{brand}_{suffix}" for brand in BRANDS for _, suffix, _ in AUDIENCES}
        stale = [k for k in keys if k not in wanted]
        if stale:
            ids = list(await session.scalars(select(Segment.id).where(Segment.key.in_(stale))))
            runs = list(await session.scalars(select(ActivationRun.id).where(
                ActivationRun.segment_id.in_(ids))))
            if runs:
                await session.execute(
                    delete(ActivationDelivery).where(ActivationDelivery.run_id.in_(runs)))
                await session.execute(delete(ActivationRun).where(ActivationRun.id.in_(runs)))
                print(f"discarded {len(runs)} past run(s) belonging to the retired audiences")
            await session.execute(delete(SegmentMember).where(SegmentMember.segment_id.in_(ids)))
            await session.execute(delete(Segment).where(Segment.id.in_(ids)))
            print(f"removed {len(stale)}: {', '.join(sorted(stale))}")

        made = 0
        for brand in BRANDS:
            for label, suffix, definition in AUDIENCES:
                key = f"{brand}_{suffix}"
                row = await session.scalar(select(Segment).where(Segment.key == key))
                if row is None:
                    row = Segment(key=key)
                    session.add(row)
                    made += 1
                row.name = label
                row.definition = definition
                row.brand = brand
                # Every one of these is for messaging, so the WhatsApp grant is
                # the gate. It is re-checked per person at send time regardless.
                row.required_consent = "marketing_whatsapp"
                row.description = f"{label} — {brand.title()}"
        await session.commit()
        print(f"{made} created, {len(BRANDS) * len(AUDIENCES)} total")


if __name__ == "__main__":
    asyncio.run(main())
