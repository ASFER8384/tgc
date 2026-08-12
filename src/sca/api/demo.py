"""Something to order, on one button.

Separate from the real endpoints and refused outside a local environment,
because the one thing worse than having no data to demonstrate with is inventing
some in production.

It used to invent a product. That was the wrong aid: a fabricated SKU has no
customers behind it, so it could only ever be planned from a typed forecast —
the button demonstrated the half of the system we are least proud of, and left a
fake row sitting in the catalogue afterwards. Now it draws down the stock of a
real item instead. Nothing is created, the demand behind the line is genuine,
and putting the stock back is one number.
"""

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from sca.api.deps import ActorDep, SessionDep
from sca.config import get_settings
from sca.models import Item, StockSnapshot, Supplier
from sca.planning.demand import weekly_demand

router = APIRouter(prefix="/demo", tags=["demo"])

# Comfortably under the four week reorder trigger, and not so low it reads as a
# catastrophe rather than a restock.
TARGET_COVER_WEEKS = 2.5


@router.post("/sample-data", status_code=status.HTTP_201_CREATED)
async def load_sample_data(session: SessionDep, actor: ActorDep) -> dict:
    """Push one real, well-stocked item below cover.

    Picks whichever item has the most headroom, so repeated presses walk down
    the catalogue instead of fighting over the same row.
    """
    settings = get_settings()
    if not settings.allow_sample_data:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "sample data is switched off: set SCA_ALLOW_SAMPLE_DATA to enable it",
        )

    suppliers = {s.id: s for s in await session.scalars(select(Supplier))}
    items = {i.sku: i for i in await session.scalars(select(Item))}
    stock = {s.sku: s for s in await session.scalars(select(StockSnapshot))}
    observed = await weekly_demand(session, now=datetime.now(UTC))

    # Only items customers actually buy. Demand is what makes the resulting line
    # defensible, and an item with none cannot produce one.
    candidates = []
    for sku, demand in observed.items():
        item, snapshot = items.get(sku), stock.get(sku)
        if item is None or snapshot is None or demand.weekly <= 0:
            continue
        supplier = suppliers.get(item.supplier_id)
        if supplier is None or not supplier.active:
            continue
        cover = (snapshot.on_hand + snapshot.on_order) / demand.weekly
        if cover > settings.reorder_cover_weeks:
            candidates.append((cover, sku, item, snapshot, supplier, demand))

    if not candidates:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "every item with sales behind it is already below cover — "
            "draft those orders first, or seed more sales history",
        )

    cover, sku, item, snapshot, supplier, demand = max(candidates, key=lambda c: c[0])
    snapshot.on_hand = int(TARGET_COVER_WEEKS * demand.weekly)
    snapshot.on_order = 0
    await session.flush()

    return {
        "items": 1,
        "written": [{"sku": sku, "supplier": supplier.name}],
        "weeks_cover": TARGET_COVER_WEEKS,
        "was_weeks_cover": round(cover, 1),
        "weekly_demand": round(demand.weekly, 1),
        "name": item.name,
    }
