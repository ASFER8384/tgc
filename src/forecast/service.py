"""Train, judge, forecast, publish.

The order matters and is the whole design. Nothing is forecast before something
has been judged, and nothing is published before it has beaten the estimator it
would replace. A run that fails is still written down, with its numbers, because
the reason a model lost this month is the most useful thing anybody has when
deciding what to change before next month.

Publishing means one specific, small thing: writing the weekly figure into
``StockSnapshot.weekly_forecast``, which is the field the buying desk already
plans against. No new path into procurement, no new approval, no automation
placing orders. A person still presses Draft, and every quantity is still
editable — the model changes where the number comes from and nothing else.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import pandas as pd
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from forecast import features as feature_module
from forecast import panel as panel_module
from forecast.evaluate import Evaluation, holdout, item_history, trailing_average
from forecast.models import ForecastBuyer, ForecastItem, ForecastRun
from sca.models import AuditLog, StockSnapshot

# How many customers to keep per item. The head of a ranked list is a shortlist
# somebody can act on; the whole list is a database dump of everybody's
# likelihood of buying everything.
TOP_BUYERS = 25


@dataclass
class RunResult:
    run: ForecastRun
    evaluation: Evaluation
    items: list[dict]
    buyers: dict[str, list[dict]]
    published: bool
    weeks_ahead: int


async def run_forecast(
    session: AsyncSession,
    *,
    actor: str,
    timezone: str = "Asia/Riyadh",
    horizon: int = 1,
    weeks_ahead: int = 8,
    test_weeks: int = 4,
    publish: bool = True,
) -> RunResult:
    """One full cycle. Returns everything the console needs to explain itself."""
    now = datetime.now(UTC)
    panel = await panel_module.build(session, now=now, timezone=timezone)
    frame = feature_module.build(panel, horizon=horizon, future_weeks=weeks_ahead)

    model, evaluation = holdout(frame, test_weeks=test_weeks)

    run = ForecastRun(
        ran_at=now,
        actor=actor,
        horizon_weeks=horizon,
        weeks_history=panel.weeks,
        train_rows=evaluation.train_rows,
        passed=evaluation.passes,
        # Only ever the reason a run was stopped. A note attached to a run that
        # passed belongs with the metrics, not in a field the console renders as
        # "this did not ship".
        refusal=(
            evaluation.refusal or ("; ".join(evaluation.reasons) or None)
            if not evaluation.passes else None
        ),
        metrics=evaluation.as_dict(),
        params=(model.params if model is not None else {}),
    )
    session.add(run)
    await session.flush()

    if model is None or not evaluation.passes:
        # Nothing forecast and nothing published. The trailing average keeps
        # running exactly as before, which is the point of the gate: a bad month
        # for the model is a quiet month for everybody else.
        session.add(AuditLog(
            actor=actor, action="forecast.rejected", entity="forecast_run", entity_id=run.id,
            meta={"refusal": run.refusal, "metrics": run.metrics},
        ))
        return RunResult(run=run, evaluation=evaluation, items=[], buyers={},
                         published=False, weeks_ahead=weeks_ahead)

    # Retrained on everything, including the weeks held back for scoring. The
    # holdout answered whether this model works; there is no reason to serve a
    # version that has not seen the most recent month.
    from forecast.model import train as train_model

    final, refusal = train_model(frame.train, horizon=horizon)
    if final is None:
        run.passed = False
        run.refusal = refusal
        return RunResult(run=run, evaluation=evaluation, items=[], buyers={},
                         published=False, weeks_ahead=weeks_ahead)

    future = frame.future.copy()
    future["probability"] = final.probability(future)
    future["quantity"] = final.quantity(future)
    future["expected"] = future["probability"] * future["quantity"]

    per_item = (
        future.groupby(["sku", "week"], observed=True)
        .agg(units=("expected", "sum"), buyers=("probability", "sum"))
        .reset_index()
        .sort_values(["sku", "week"])
    )

    # The same corrections the gate scored, applied to what is actually served. A
    # model judged with a correction and then served without it is not the thing
    # that was judged, and the difference would show up as the forecast being
    # wrong in exactly the way the holdout said it had stopped being wrong.
    calibration = evaluation.calibration
    history = item_history(frame)
    first_future = frame.future_weeks[0] if frame.future_weeks else panel.last_week
    baseline_by_sku = {
        str(sku): trailing_average(history, sku=str(sku), before=first_future)
        for sku in per_item["sku"].unique()
    }
    per_item["units"] = [
        calibration.apply(row.units, baseline_by_sku.get(str(row.sku), 0.0))
        for row in per_item.itertuples()
    ]
    for row in per_item.itertuples():
        session.add(ForecastItem(
            run_id=run.id, sku=str(row.sku), week=row.week,
            units=float(row.units), buyers=float(row.buyers),
        ))

    # Who will buy, over the whole horizon rather than in one particular week: the
    # question a campaign asks is "who is likely to buy this soon", and pinning it
    # to a single week would drop somebody whose likeliest week is the next one.
    # The level correction applies here too — it is a statement about how much
    # the model overshoots, and it overshoots per customer as much as per item.
    # The blend does not: mixing one person's likelihood with a shelf-level
    # average would produce a number about nobody.
    future["expected"] = future["expected"] * calibration.factor
    per_person = (
        future.groupby(["sku", "person_id"], observed=True)
        .agg(probability=("probability", "max"), expected=("expected", "sum"))
        .reset_index()
    )
    buyers: dict[str, list[dict]] = {}
    for sku, part in per_person.groupby("sku", observed=True):
        top = part.sort_values("expected", ascending=False).head(TOP_BUYERS)
        buyers[str(sku)] = []
        for row in top.itertuples():
            session.add(ForecastBuyer(
                run_id=run.id, sku=str(sku), person_id=str(row.person_id),
                probability=float(row.probability), expected_units=float(row.expected),
            ))
            buyers[str(sku)].append({
                "person_id": str(row.person_id),
                "probability": round(float(row.probability), 4),
                "expected_units": round(float(row.expected), 2),
            })

    items = _item_rows(per_item)
    if publish:
        await _publish(session, per_item, panel, actor=actor, run_id=run.id)
        run.published = True

    session.add(AuditLog(
        actor=actor, action="forecast.published" if publish else "forecast.accepted",
        entity="forecast_run", entity_id=run.id,
        meta={"metrics": run.metrics, "items": len(items)},
    ))
    await session.flush()

    return RunResult(run=run, evaluation=evaluation, items=items, buyers=buyers,
                     published=bool(publish), weeks_ahead=weeks_ahead)


def _item_rows(per_item: pd.DataFrame) -> list[dict]:
    """Per item: the weekly path, the next week, and the next four weeks summed.

    Both horizons because they answer different questions. Next week is what the
    shop will sell; the month is what has to be on a boat.
    """
    out = []
    for sku, part in per_item.groupby("sku", observed=True):
        part = part.sort_values("week")
        weekly = [
            {"week": row.week.isoformat(), "units": round(float(row.units), 2),
             "buyers": round(float(row.buyers), 2)}
            for row in part.itertuples()
        ]
        out.append({
            "sku": str(sku),
            "weekly": weekly,
            "next_week": round(float(part["units"].iloc[0]), 2) if len(part) else 0.0,
            "next_month": round(float(part["units"].head(4).sum()), 2),
            "buyers_next_month": round(float(part["buyers"].head(4).sum()), 2),
        })
    return sorted(out, key=lambda r: r["next_month"], reverse=True)


async def _publish(
    session: AsyncSession,
    per_item: pd.DataFrame,
    panel,
    *,
    actor: str,
    run_id: str,
) -> None:
    """Write the weekly figure where the buying desk already reads it.

    An average of the coming four weeks rather than next week alone: the field is
    a weekly rate that the planner multiplies by a cover target, and handing it a
    single week would make every reorder decision swing on one noisy number.

    The thresholds go with it. When to reorder and how much cover to buy up to
    are derived from the mill's lead time, this line's own week-to-week spread
    and the minimum it has to be bought in — all measured, all already here, and
    none of them reaching the decision while the desk used one constant for the
    whole catalogue. Writing them beside the rate keeps the buying half free of
    any knowledge of the forecasting half, and keeps the two from disagreeing
    about a line.
    """
    from forecast import guidance as guidance_module

    # The rate first, for every line. The thresholds are computed from it — a
    # safety term needs the demand it is protecting and a cycle needs the rate
    # the minimum is divided by — so deriving them before the write would size
    # this run's thresholds against the last run's demand.
    changed = []
    touched = []
    for sku, part in per_item.groupby("sku", observed=True):
        weekly = float(part.sort_values("week")["units"].head(4).mean())
        snapshot = await session.get(StockSnapshot, str(sku))
        if snapshot is None:
            # No stock record means nobody is buying this line yet. A forecast
            # against a SKU the supplier half has never seen is not actionable,
            # and inventing a stock row to hold it would put a zero on-hand into
            # planning as though somebody had counted it.
            continue
        before = float(snapshot.weekly_forecast or 0)
        snapshot.weekly_forecast = Decimal(str(round(weekly, 2)))
        # Signed, so the desk can say where the rate came from. Without this the
        # figure written here was read back as something a person had typed.
        snapshot.weekly_forecast_source = "model"
        touched.append((str(sku), snapshot))
        changed.append({"sku": str(sku), "from": before, "to": round(weekly, 2)})

    # Flushed so the guidance pass reads the rates just written rather than the
    # ones they replaced.
    await session.flush()
    advice = await guidance_module.build(session, panel)
    by_sku = {row["sku"]: row for row in changed}
    for sku, snapshot in touched:
        hint = advice.get(sku)
        if not hint or not hint.suggested_reorder_weeks or not hint.suggested_target_weeks:
            # Nothing derivable — no lead time, or no rate to divide by. Left
            # null so the planner falls back to the deployment default for this
            # line alone, rather than carrying a stale pair from a run when it
            # could be derived.
            snapshot.model_reorder_weeks = None
            snapshot.model_target_weeks = None
            snapshot.model_threshold_basis = None
            continue
        snapshot.model_reorder_weeks = Decimal(str(hint.suggested_reorder_weeks))
        snapshot.model_target_weeks = Decimal(str(hint.suggested_target_weeks))
        snapshot.model_threshold_basis = (hint.threshold_basis or "")[:120]
        by_sku[sku]["reorder_weeks"] = hint.suggested_reorder_weeks
        by_sku[sku]["target_weeks"] = hint.suggested_target_weeks

    session.add(AuditLog(
        actor=actor, action="forecast.weekly", entity="forecast_run", entity_id=run_id,
        meta={"changed": changed},
    ))


async def latest(session: AsyncSession) -> ForecastRun | None:
    return await session.scalar(
        select(ForecastRun).order_by(ForecastRun.ran_at.desc()).limit(1)
    )


async def latest_published(session: AsyncSession) -> ForecastRun | None:
    return await session.scalar(
        select(ForecastRun)
        .where(ForecastRun.published.is_(True))
        .order_by(ForecastRun.ran_at.desc())
        .limit(1)
    )
