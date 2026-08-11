"""Suppliers, items and the stock position they are bought against."""

from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from sca.api.deps import ActorDep, SessionDep
from sca.models import Item, StockSnapshot, Supplier
from sca.planning.demand import weekly_demand
from sca.scheduling.windows import WorkingHours, is_open, local_now, next_open

router = APIRouter(tags=["catalog"])


class SupplierIn(BaseModel):
    code: str
    name: str
    country: str | None = None
    email: str | None = None
    channel: str = "email"
    timezone: str = "Asia/Riyadh"
    working_days: str = Field(default="1,2,3,4,5", description="ISO weekdays, Monday is 1")
    work_start_hour: int = 9
    work_end_hour: int = 17
    lead_time_days: int = 21
    currency: str = "SAR"
    min_order_value: Decimal = Decimal("0")


class SupplierOut(BaseModel):
    id: str
    code: str
    name: str
    country: str | None
    email: str | None
    timezone: str
    lead_time_days: int
    currency: str
    # Derived, not stored: the two things a buyer wants at a glance before
    # deciding whether to call or to queue a message.
    local_time: str
    open_now: bool
    next_open_local: str


class ItemIn(BaseModel):
    sku: str
    name: str
    supplier_id: str
    category: str = "finished_goods"
    brand: str | None = None
    unit: str = "pcs"
    moq: int = 1
    pack_size: int = 1
    unit_cost: Decimal = Decimal("0")


class StockIn(BaseModel):
    sku: str
    on_hand: int = 0
    on_order: int = 0
    weekly_forecast: Decimal = Decimal("0")


@router.post("/suppliers", response_model=SupplierOut, status_code=status.HTTP_201_CREATED)
async def upsert_supplier(body: SupplierIn, session: SessionDep, actor: ActorDep) -> SupplierOut:
    supplier = await session.scalar(select(Supplier).where(Supplier.code == body.code))
    if supplier is None:
        supplier = Supplier(code=body.code, name=body.name)
        session.add(supplier)
    for field_name, value in body.model_dump().items():
        setattr(supplier, field_name, value)
    await session.flush()
    return _supplier_out(supplier)


@router.get("/suppliers", response_model=list[SupplierOut])
async def list_suppliers(session: SessionDep, actor: ActorDep) -> list[SupplierOut]:
    rows = await session.scalars(select(Supplier).order_by(Supplier.name))
    return [_supplier_out(s) for s in rows]


@router.post("/items", status_code=status.HTTP_201_CREATED)
async def upsert_item(body: ItemIn, session: SessionDep, actor: ActorDep) -> dict:
    item = await session.scalar(select(Item).where(Item.sku == body.sku))
    if item is None:
        item = Item(sku=body.sku, name=body.name, supplier_id=body.supplier_id)
        session.add(item)
    for field_name, value in body.model_dump().items():
        setattr(item, field_name, value)
    await session.flush()
    return {"sku": item.sku, "name": item.name}


@router.get("/items")
async def list_items(session: SessionDep, actor: ActorDep) -> list[dict]:
    items = await session.scalars(select(Item).order_by(Item.sku))
    stock = {s.sku: s for s in await session.scalars(select(StockSnapshot))}
    # Both numbers, always: the typed forecast is what drives buying, and the
    # measured one is what lets someone notice the typed figure has gone stale.
    observed = await weekly_demand(session, now=datetime.now(UTC))
    out = []
    for item in items:
        snapshot = stock.get(item.sku)
        manual = float(snapshot.weekly_forecast) if snapshot else 0.0
        measured = observed.get(item.sku)
        weekly = manual if manual > 0 else (measured.weekly if measured else 0.0)
        available = (snapshot.on_hand + snapshot.on_order) if snapshot else 0
        out.append({
            "sku": item.sku,
            "name": item.name,
            "category": item.category,
            "brand": item.brand,
            "supplier_id": item.supplier_id,
            "unit_cost": str(item.unit_cost),
            "on_hand": snapshot.on_hand if snapshot else 0,
            "on_order": snapshot.on_order if snapshot else 0,
            "weekly_forecast": weekly,
            "forecast_source": "manual" if manual > 0 else ("sales" if weekly else "none"),
            "observed_weekly": round(measured.weekly, 1) if measured else None,
            "weeks_cover": round(available / weekly, 1) if weekly else None,
        })
    return out


@router.post("/stock", status_code=status.HTTP_201_CREATED)
async def upsert_stock(body: StockIn, session: SessionDep, actor: ActorDep) -> dict:
    """The handoff from the inventory and forecasting module.

    Kept as a plain upsert so that module can push whenever it recalculates,
    without this service needing to know how the forecast was produced.
    """
    snapshot = await session.get(StockSnapshot, body.sku)
    if snapshot is None:
        snapshot = StockSnapshot(sku=body.sku)
        session.add(snapshot)
    snapshot.on_hand = body.on_hand
    snapshot.on_order = body.on_order
    snapshot.weekly_forecast = body.weekly_forecast
    await session.flush()
    return {"sku": body.sku, "on_hand": body.on_hand, "weekly_forecast": str(body.weekly_forecast)}


def _supplier_out(supplier: Supplier) -> SupplierOut:
    hours = WorkingHours.from_supplier(supplier)
    now = datetime.now(UTC)
    return SupplierOut(
        id=supplier.id,
        code=supplier.code,
        name=supplier.name,
        country=supplier.country,
        email=supplier.email,
        timezone=supplier.timezone,
        lead_time_days=supplier.lead_time_days,
        currency=supplier.currency,
        local_time=local_now(hours, now).strftime("%H:%M"),
        open_now=is_open(hours, now),
        next_open_local=next_open(hours, now).astimezone(hours.zone).strftime("%a %H:%M"),
    )


async def get_supplier_or_404(session, supplier_id: str) -> Supplier:
    supplier = await session.get(Supplier, supplier_id)
    if supplier is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown supplier")
    return supplier
