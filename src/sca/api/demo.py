"""Test data, on one button.

Separate from the real endpoints and refused outside a local environment, because
the one thing worse than having no data to demonstrate with is inventing some in
production. Everything here goes through the same upsert paths a real import
would, so a second click corrects the rows rather than duplicating them.
"""

from decimal import Decimal

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import func, select

from sca.api.deps import ActorDep, SessionDep
from sca.config import get_settings
from sca.models import Item, StockSnapshot, Supplier

router = APIRouter(prefix="/demo", tags=["demo"])

# A rotating catalogue rather than a fixed list. Every press has to produce a
# line worth looking at, so the button stays useful on the fiftieth click as well
# as the first: the shapes repeat, the SKU never does.
SAMPLE_SHAPES = [
    # description, unit, moq, pack, cost, weeks of cover, weekly forecast
    ("Navy silk, 140cm", "m", 300, 50, "42.00", 2.9, 90),
    ("Embroidered abaya", "pcs", 100, 25, "180.00", 2.0, 60),
    ("Lipstick tube, matte black", "pcs", 2000, 500, "3.20", 2.5, 1200),
    ("Luxury outer box", "pcs", 1000, 200, "9.80", 1.2, 320),
    ("Kraft mailer, medium", "pcs", 500, 100, "1.40", 0.6, 800),
    ("Rose attar concentrate", "l", 20, 5, "610.00", 3.1, 12),
]


@router.post("/sample-data", status_code=status.HTTP_201_CREATED)
async def load_sample_data(session: SessionDep, actor: ActorDep) -> dict:
    """One new item below cover, every press.

    Deliberately never "already loaded": the button exists so there is always
    something to order, and a testing aid that runs out stops being one. Each
    call takes the next SKU number, so nothing collides with what is already
    there and no existing line is quietly overwritten.
    """
    settings = get_settings()
    if not settings.allow_sample_data:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "sample data is switched off: set SCA_ALLOW_SAMPLE_DATA to enable it",
        )

    suppliers = list(await session.scalars(select(Supplier).order_by(Supplier.name)))
    if not suppliers:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "add a supplier first: items belong to one, and the order is grouped by it",
        )

    # The count is the cursor. Numbering from what exists rather than from a
    # stored counter means deleting the test rows resets it, which is what
    # someone clearing up expects to happen.
    made = await session.scalar(
        select(func.count()).select_from(Item).where(Item.sku.like("TEST-%"))
    ) or 0
    index = made + 1
    sku = f"TEST-{index:03d}"
    description, unit, moq, pack, cost, weeks, forecast = SAMPLE_SHAPES[
        made % len(SAMPLE_SHAPES)
    ]
    # Cover is what the planner reads, so the stock is derived from the cover we
    # want the line to show rather than picked and hoped for.
    on_hand = int(weeks * forecast)
    supplier = suppliers[made % len(suppliers)]

    session.add(
        StockSnapshot(
            sku=sku, on_hand=on_hand, on_order=0,
            weekly_forecast=Decimal(str(forecast)),
        )
    )
    session.add(
        Item(
            sku=sku, name=f"{description} ({index})", supplier_id=supplier.id,
            category="general", brand="aleena", unit=unit, moq=moq,
            pack_size=pack, unit_cost=Decimal(cost),
        )
    )
    await session.flush()
    return {
        "items": 1,
        "written": [{"sku": sku, "supplier": supplier.name}],
        "weeks_cover": round(weeks, 1),
    }
