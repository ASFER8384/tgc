"""Suppliers, items and the stock position they are bought against."""

from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from sca.api.deps import ActorDep, SessionDep
from sca.models import (
    AuditLog,
    Issue,
    Item,
    StockLevel,
    StockSnapshot,
    Supplier,
    SupplierItem,
)
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
    # Carried so the console can open an existing supplier in the same form that
    # created them. Without these the edit dialog would have to guess at the
    # working week, and saving would quietly overwrite it with the default.
    working_days: str
    work_start_hour: int
    work_end_hour: int
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
    # When this position was true, where that is not now. An upstream system
    # replaying a backlog is stating history, and stamping it with the moment it
    # happened to be sent would put the whole backlog in one instant.
    recorded_at: datetime | None = None


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


class SupplierItemIn(BaseModel):
    sku: str
    # All optional: attaching a SKU is usually done at onboarding, when somebody
    # knows the mill makes abayas and has not yet negotiated a price. Blank
    # inherits whatever the item already carries rather than writing a zero that
    # would later read as free.
    unit_cost: Decimal | None = None
    currency: str | None = None
    moq: int | None = None
    pack_size: int | None = None
    lead_time_days: int | None = None


class SupplierItemsIn(BaseModel):
    """The full list of what this supplier makes. Sent whole, so unticking a SKU
    in the console removes it — a partial list would make removal impossible."""

    items: list[SupplierItemIn]


@router.put("/suppliers/{code}/items")
async def set_supplier_items(
    code: str, body: SupplierItemsIn, session: SessionDep, actor: ActorDep
) -> dict:
    """What this supplier can actually deliver.

    Nobody makes everything: one mill does the abaya, another only the
    packaging. Until this existed an item named exactly one supplier, so there
    was nothing to compare and no way to route an order to whoever makes it.
    """
    supplier = await session.scalar(select(Supplier).where(Supplier.code == code))
    if supplier is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown supplier {code}")

    known = {i.sku: i for i in await session.scalars(select(Item))}
    unknown = [row.sku for row in body.items if row.sku not in known]
    if unknown:
        # Refused rather than created: an item invented from a typo here would
        # have no cost, no pack size and no name, and would sit in planning
        # looking like a real product nobody can buy.
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"no such item: {', '.join(sorted(unknown))}",
        )

    existing = {
        row.sku: row
        for row in await session.scalars(
            select(SupplierItem).where(SupplierItem.supplier_id == supplier.id)
        )
    }
    wanted = {row.sku for row in body.items}
    for sku, row in existing.items():
        if sku not in wanted:
            await session.delete(row)

    for row in body.items:
        item = known[row.sku]
        link = existing.get(row.sku)
        if link is None:
            link = SupplierItem(supplier_id=supplier.id, sku=row.sku)
            session.add(link)
        link.unit_cost = row.unit_cost if row.unit_cost is not None else item.unit_cost
        link.currency = (row.currency or supplier.currency).upper()
        link.moq = row.moq if row.moq is not None else item.moq
        link.pack_size = row.pack_size if row.pack_size is not None else item.pack_size
        link.lead_time_days = row.lead_time_days
        link.active = True

    session.add(
        AuditLog(
            actor=actor, action="supplier.items", entity="supplier", entity_id=supplier.id,
            meta={"skus": sorted(wanted)},
        )
    )
    await session.flush()
    return {"supplier": supplier.code, "skus": sorted(wanted)}


@router.get("/suppliers/{code}/items")
async def get_supplier_items(code: str, session: SessionDep, actor: ActorDep) -> list[dict]:
    supplier = await session.scalar(select(Supplier).where(Supplier.code == code))
    if supplier is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown supplier {code}")
    rows = await session.scalars(
        select(SupplierItem)
        .where(SupplierItem.supplier_id == supplier.id)
        .order_by(SupplierItem.sku)
    )
    return [
        {
            "sku": row.sku, "unit_cost": str(row.unit_cost), "currency": row.currency,
            "moq": row.moq, "pack_size": row.pack_size, "lead_time_days": row.lead_time_days,
        }
        for row in rows
    ]


@router.get("/items/{sku}/suppliers")
async def item_suppliers(sku: str, session: SessionDep, actor: ActorDep) -> list[dict]:
    """Everyone who can make this, with what each would charge and how long.

    Not ranked into a winner. The cheapest, the fastest and the most reliable
    are rarely the same mill, and which one matters depends on whether this is
    covering a stockout or a launch — a judgement the buyer holds and the
    system does not.
    """
    rows = list(
        await session.scalars(
            select(SupplierItem).where(SupplierItem.sku == sku, SupplierItem.active.is_(True))
        )
    )
    if not rows:
        return []
    suppliers = {
        s.id: s
        for s in await session.scalars(
            select(Supplier).where(Supplier.id.in_([r.supplier_id for r in rows]))
        )
    }
    # Their record, from issues this system already raised. Reliability, not
    # quality: nothing here inspects the goods, and calling it quality would be
    # claiming a measurement nobody takes.
    trouble = dict(
        (
            await session.execute(
                select(Issue.supplier_id, func.count(Issue.id))
                .where(Issue.supplier_id.in_(list(suppliers)))
                .group_by(Issue.supplier_id)
            )
        ).all()
    )
    out = []
    for row in rows:
        supplier = suppliers.get(row.supplier_id)
        if supplier is None:
            continue
        out.append({
            "supplier_id": supplier.id,
            "supplier": supplier.name,
            "code": supplier.code,
            "country": supplier.country,
            "unit_cost": str(row.unit_cost),
            "currency": row.currency,
            "moq": row.moq,
            "pack_size": row.pack_size,
            "lead_time_days": row.lead_time_days or supplier.lead_time_days,
            "lead_time_is_theirs": row.lead_time_days is not None,
            "issues_raised": int(trouble.get(supplier.id, 0)),
        })
    # Cheapest first as a starting order only. Currencies may differ, which is
    # why the currency travels with every price instead of being folded away.
    out.sort(key=lambda r: Decimal(r["unit_cost"]))
    return out


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
            # The terms an order is rounded to. Reported so the console can edit
            # an item without first inventing them: a blank minimum saved back
            # would silently become 1 and start drafting orders no mill accepts.
            "unit": item.unit,
            "moq": item.moq,
            "pack_size": item.pack_size,
            "on_hand": snapshot.on_hand if snapshot else 0,
            "on_order": snapshot.on_order if snapshot else 0,
            "weekly_forecast": weekly,
            # The effective figure above can be either source, so the typed one
            # is reported separately. Collapsing them would make a row driven by
            # sales look like it had a forecast entered that happened to agree.
            "entered_weekly": manual if manual > 0 else None,
            "forecast_source": "manual" if manual > 0 else ("sales" if weekly else "none"),
            "observed_weekly": round(measured.weekly, 1) if measured else None,
            # Whether the sold-per-week figure had stockouts taken out of it.
            # An uncorrected one is an under-estimate by an unknown amount, and
            # the row should not look identical to a corrected one.
            "observed_availability": measured.availability if measured else None,
            "observed_stockout_weeks": (
                round(measured.stockout_weeks, 1) if measured else None
            ),
            "weeks_cover": round(available / weekly, 1) if weekly else None,
        })
    return out


@router.post("/stock", status_code=status.HTTP_201_CREATED)
async def upsert_stock(body: StockIn, session: SessionDep, actor: ActorDep) -> dict:
    """The handoff from the inventory and forecasting module.

    Kept as a plain upsert so that module can push whenever it recalculates,
    without this service needing to know how the forecast was produced.

    The position is also appended to the ledger, which is what lets demand be
    measured over the weeks an item was actually sellable rather than over the
    whole window. A push that repeats the current figures is not appended: the
    last reading holds forward on its own, and storing it again would only make
    the ledger longer.
    """
    snapshot = await session.get(StockSnapshot, body.sku)
    known = snapshot is not None
    if snapshot is None:
        snapshot = StockSnapshot(sku=body.sku)
        session.add(snapshot)
    moved = not known or snapshot.on_hand != body.on_hand or snapshot.on_order != body.on_order
    snapshot.on_hand = body.on_hand
    snapshot.on_order = body.on_order
    snapshot.weekly_forecast = body.weekly_forecast
    if moved:
        session.add(
            StockLevel(
                sku=body.sku,
                on_hand=body.on_hand,
                on_order=body.on_order,
                recorded_at=body.recorded_at or datetime.now(UTC),
            )
        )
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
        working_days=supplier.working_days,
        work_start_hour=supplier.work_start_hour,
        work_end_hour=supplier.work_end_hour,
        local_time=local_now(hours, now).strftime("%H:%M"),
        open_now=is_open(hours, now),
        next_open_local=next_open(hours, now).astimezone(hours.zone).strftime("%a %H:%M"),
    )


async def get_supplier_or_404(session, supplier_id: str) -> Supplier:
    supplier = await session.get(Supplier, supplier_id)
    if supplier is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown supplier")
    return supplier
