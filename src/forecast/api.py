"""The forecast, as the console reads it."""

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select

from cdp.models import Identifier, Person
from forecast.models import ForecastBuyer, ForecastItem, ForecastRun
from sca.api.deps import ActorDep, RuntimeSettingsDep, SessionDep
from sca.models import Item, StockSnapshot

router = APIRouter(prefix="/forecast", tags=["forecast"])


def _service():
    """The training stack, imported on first use rather than at start-up.

    ``forecast.service`` pulls in pandas, numpy, scikit-learn and LightGBM —
    hundreds of megabytes that the rest of the platform never touches. Imported
    at module scope they are loaded by every process that serves any route at
    all, and a deployment whose build cannot fit them refuses to start rather
    than starting without a forecast. The buying desk, the customer console and
    the brand module do not need a gradient booster to answer a GET.
    """
    from forecast import service

    return service


class RunIn(BaseModel):
    # How far ahead the model is asked to see. One week is the honest default:
    # every extra week withholds another week of history from the features, and
    # on a short trading record that is expensive.
    horizon: int = 1
    weeks_ahead: int = 8
    test_weeks: int = 4
    # Whether the result reaches the buying desk. Off is a real thing to want —
    # somebody trying a horizon should not move the numbers a buyer is working
    # from that morning.
    publish: bool = True


@router.post("/run")
async def run_now(
    body: RunIn, session: SessionDep, actor: ActorDep, settings: RuntimeSettingsDep
) -> dict:
    """Train, score against the incumbent, and publish only if it wins."""
    result = await _service().run_forecast(
        session,
        actor=actor,
        timezone=settings.home_timezone,
        horizon=max(1, min(body.horizon, 12)),
        weeks_ahead=max(1, min(body.weeks_ahead, 26)),
        test_weeks=max(2, min(body.test_weeks, 12)),
        publish=body.publish,
    )
    return {
        "run_id": result.run.id,
        "passed": result.run.passed,
        "published": result.published,
        "refusal": result.run.refusal,
        "metrics": result.run.metrics,
        "items": result.items,
    }


@router.get("/summary")
async def summary(
    session: SessionDep, actor: ActorDep, settings: RuntimeSettingsDep
) -> dict:
    """What will sell, how many, and what that means for stock.

    Reads the last run rather than training on the way in: a page load is not a
    reason to retrain, and a forecast that changes every time somebody refreshes
    is not a forecast anybody can act on.
    """
    run = await _service().latest(session)
    if run is None:
        return {"state": "never_run", "run": None, "items": []}

    rows = list(await session.scalars(
        select(ForecastItem).where(ForecastItem.run_id == run.id).order_by(ForecastItem.week)
    ))
    items = {i.sku: i for i in await session.scalars(select(Item))}
    stock = {s.sku: s for s in await session.scalars(select(StockSnapshot))}

    by_sku: dict[str, list[ForecastItem]] = {}
    for row in rows:
        by_sku.setdefault(row.sku, []).append(row)

    out = []
    for sku, weeks in by_sku.items():
        weeks.sort(key=lambda r: r.week)
        item = items.get(sku)
        snapshot = stock.get(sku)
        next_week = float(weeks[0].units) if weeks else 0.0
        month = float(sum(w.units for w in weeks[:4]))
        weekly = month / min(4, len(weeks)) if weeks else 0.0
        available = (snapshot.on_hand + snapshot.on_order) if snapshot else 0
        # Weeks of cover against the forecast rather than against history — which
        # is the entire point of having one. A line with eight weeks of cover
        # against last month's rate can still be short if next month is Eid.
        cover = (available / weekly) if weekly > 0 else None
        # What is missing before the target cover is reached. Reported, not
        # ordered: the buying desk turns this into a draft and a person approves
        # it, exactly as it does today.
        target = float(
            item.target_cover_weeks if item is not None and item.target_cover_weeks is not None
            else settings.target_cover_weeks
        )
        shortfall = max(0.0, target * weekly - available)
        out.append({
            "sku": sku,
            "name": item.name if item else None,
            "brand": item.brand if item else None,
            "next_week": round(next_week, 2),
            "next_month": round(month, 2),
            "weekly": round(weekly, 2),
            "buyers_next_month": round(float(sum(w.buyers for w in weeks[:4])), 2),
            "weekly_path": [
                {"week": w.week.isoformat(), "units": round(float(w.units), 2)} for w in weeks
            ],
            "on_hand": snapshot.on_hand if snapshot else 0,
            "on_order": snapshot.on_order if snapshot else 0,
            "weeks_cover": round(cover, 1) if cover is not None else None,
            "target_cover_weeks": target,
            "suggested_order": int(round(shortfall)),
            "moq": item.moq if item else None,
            "pack_size": item.pack_size if item else None,
            "unknown_to_procurement": snapshot is None,
        })

    out.sort(key=lambda r: r["next_month"], reverse=True)
    return {
        "state": "published" if run.published else ("passed" if run.passed else "rejected"),
        "run": _run_out(run),
        "items": out,
    }


@router.get("/buyers/{sku}")
async def buyers(sku: str, session: SessionDep, actor: ActorDep) -> dict:
    """Who is most likely to buy this, and roughly how much.

    Names are resolved here and nowhere else. The forecast module reads people and
    the supplier half does not — a buyer approving a purchase order has no reason
    to see who bought what, so this list is reachable from the customer side of
    the console only.
    """
    run = await _service().latest(session)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no forecast has been run yet")

    rows = list(await session.scalars(
        select(ForecastBuyer)
        .where(ForecastBuyer.run_id == run.id, ForecastBuyer.sku == sku)
        .order_by(ForecastBuyer.expected_units.desc())
    ))
    if not rows:
        return {"sku": sku, "buyers": []}

    ids = [r.person_id for r in rows]
    people = {p.id: p for p in await session.scalars(select(Person).where(Person.id.in_(ids)))}
    contacts: dict[str, dict[str, str]] = {}
    for identifier in await session.scalars(
        select(Identifier).where(Identifier.person_id.in_(ids))
    ):
        contacts.setdefault(identifier.person_id, {}).setdefault(identifier.kind, identifier.value)

    out = []
    for row in rows:
        person = people.get(row.person_id)
        out.append({
            "person_id": row.person_id,
            "name": person.display_name if person else None,
            "phone": contacts.get(row.person_id, {}).get("phone"),
            "email": contacts.get(row.person_id, {}).get("email"),
            "probability": round(row.probability, 4),
            "expected_units": round(row.expected_units, 2),
        })
    return {"sku": sku, "run_id": run.id, "buyers": out}


@router.get("/plan")
async def order_plan(
    session: SessionDep, actor: ActorDep, cover_months: float = 1.0
) -> dict:
    """What to order for next month, per shop and per size.

    Deliberately not served by the trained model. That one forecasts an item's
    weekly units group-wide, which is the number a gate can be held against and
    not the number anybody orders against — a mill is told a curve and a
    destination. This reads two years of trading directly, against the Saudi
    calendar, and reports its own walk-forward error beside the answer.

    No import of the training stack, so this answers whether or not a model has
    ever been fitted and whether or not LightGBM is installed.
    """
    from forecast.plan import build_plan

    return await build_plan(session, cover_months=max(0.1, min(cover_months, 6.0)))


@router.get("/runs")
async def runs(session: SessionDep, actor: ActorDep, limit: int = 20) -> list[dict]:
    """Every attempt, accepted and rejected. The rejected ones are the evidence
    for what to fix, and keeping only the winners hides the trend that says the
    data has changed."""
    rows = await session.scalars(
        select(ForecastRun).order_by(ForecastRun.ran_at.desc()).limit(max(1, min(limit, 100)))
    )
    return [_run_out(row) for row in rows]


def _run_out(run: ForecastRun) -> dict:
    metrics = run.metrics or {}
    return {
        "id": run.id,
        "ran_at": run.ran_at.isoformat(),
        "actor": run.actor,
        "horizon_weeks": run.horizon_weeks,
        "weeks_history": run.weeks_history,
        "train_rows": run.train_rows,
        "passed": run.passed,
        "published": run.published,
        "refusal": run.refusal,
        "model": metrics.get("model"),
        "baseline": metrics.get("baseline"),
        "per_item": metrics.get("per_item", {}),
        "test_weeks": metrics.get("test_weeks", []),
        # Notes attached to a run that passed — a lean worth knowing about, say.
        # Kept apart from `refusal`, which only ever explains a run that stopped.
        "notes": metrics.get("reasons", []) if run.passed else [],
        # What was corrected before the gate scored it, and by how much. On the
        # page rather than in the metrics blob: a forecast that is half trailing
        # average is a different thing to act on than one that is all model.
        "calibration": metrics.get("calibration"),
        "raw_model": metrics.get("raw_model"),
    }


@router.get("/readiness")
async def readiness(
    session: SessionDep, actor: ActorDep, settings: RuntimeSettingsDep
) -> dict:
    """Whether there is enough recorded history for any of this to mean anything.

    Asked separately because the answer is a fact about the business rather than
    about the model, and because "the forecast is poor" and "there are eleven
    weeks of data" are the same finding stated at two different levels of use.
    """
    from forecast import panel as panel_module

    built = await panel_module.build(
        session, now=datetime.now(UTC), timezone=settings.home_timezone
    )
    week_rows = sum(len(rows) for rows in built.items.values())
    with_ledger = sum(
        1 for rows in built.items.values() for row in rows if row.sellable_days is not None
    )
    return {
        "weeks": built.weeks,
        "first_week": built.first_week.isoformat() if built.first_week else None,
        "skus": len(built.items),
        "customers": len(built.people),
        "units": built.total_units,
        "anonymous_units": built.anonymous_units,
        "attributed_share": (
            round(built.attributed_share, 3) if built.attributed_share is not None else None
        ),
        "lines_without_sku": built.lines_without_sku,
        "availability_known_share": (
            round(with_ledger / week_rows, 3) if week_rows else None
        ),
    }


@router.get("/guidance")
async def guidance(
    session: SessionDep, actor: ActorDep, settings: RuntimeSettingsDep
) -> dict:
    """The evidence behind the four thresholds a buyer sets by hand.

    Read-only, and deliberately so. Those thresholds are judgements the buyer
    owns; a page that quietly filled them in would be making the judgement while
    appearing to ask for it. This puts the facts beside the box instead — the
    rate the numbers multiply, how long the mill takes, how lumpy the line is,
    and whether the model is any good at this particular item.
    """
    from forecast import guidance as guidance_module
    from forecast import panel as panel_module

    built = await panel_module.build(
        session, now=datetime.now(UTC), timezone=settings.home_timezone
    )
    run = await _service().latest_published(session) or await _service().latest(session)
    per_item = ((run.metrics or {}).get("per_item") if run else None) or {}
    rows = await guidance_module.build(session, built, last_run_per_item=per_item)
    return {
        "run_id": run.id if run else None,
        "ran_at": run.ran_at.isoformat() if run else None,
        "published": bool(run.published) if run else False,
        "items": {sku: g.as_dict() for sku, g in rows.items()},
    }
