"""What the coordination cost, as numbers rather than as a claim."""

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from sca.analytics.arrival import MODES, ArrivalService
from sca.analytics.supplier import SupplierAnalyticsService
from sca.analytics.weather import advisory
from sca.api.deps import ActorDep, RuntimeSettingsDep, SessionDep
from sca.coordination.service import CoordinationService
from sca.models import Supplier

router = APIRouter(tags=["ops"])


@router.get("/coordination")
async def coordination(
    session: SessionDep, actor: ActorDep, settings: RuntimeSettingsDep
) -> dict:
    """Round trips, cycle time by timezone, and how much ran without a human.

    Returned as one document rather than five endpoints because every number is
    read against the others: replies per order means little without knowing the
    overlap the replies had to cross.
    """
    return (await CoordinationService(session, settings=settings).collect()).as_dict()


@router.get("/suppliers/{code}/analytics")
async def supplier_analytics(code: str, session: SessionDep, actor: ActorDep) -> dict:
    """What this supplier has cost, counted from their own record.

    Not folded into a score. A single number would rank a mill that ships
    perfectly but makes one thing above a mill that is occasionally late and is
    the only source of three, and a buyer facing a stockout needs those two
    facts apart.
    """
    supplier = await session.scalar(select(Supplier).where(Supplier.code == code))
    if supplier is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown supplier {code}")
    return (await SupplierAnalyticsService(session).collect(supplier)).as_dict()


@router.get("/suppliers/{code}/arrival")
async def arrival(
    code: str,
    session: SessionDep,
    actor: ActorDep,
    settings: RuntimeSettingsDep,
    mode: str | None = None,
) -> dict:
    """If we order today, roughly when does it land — and on what grounds.

    Every mode is returned, not just the likely one, because "eight days by air
    against twenty-nine by sea" is the comparison a buyer facing a launch date
    actually makes, and it is not one they should have to ask twice for.
    """
    supplier = await session.scalar(select(Supplier).where(Supplier.code == code))
    if supplier is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown supplier {code}")
    if mode is not None and mode not in MODES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"unknown mode {mode}, have {', '.join(sorted(MODES))}",
        )

    service = ArrivalService(session, settings=settings)
    estimate = await service.estimate(supplier, mode=mode)
    if settings.weather_advisory:
        estimate.weather = await advisory(supplier.country)

    others = {}
    for name in MODES:
        if name != estimate.mode:
            other = await service.estimate(supplier, mode=name)
            others[name] = {"total_days": other.total_days, "arrives_on": other.arrives_on}
    return estimate.as_dict() | {
        "alternatives": {
            k: v | {"arrives_on": v["arrives_on"].isoformat() if v["arrives_on"] else None}
            for k, v in others.items()
        }
    }
