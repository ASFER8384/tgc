"""The gate.

The model is trained on the first part of the history and scored on weeks it was
never shown, against the eight-week trailing average that fills the forecast
column today. If it does not win, it is not published and the average keeps
running. Nothing here is advisory.

Two conditions, both required.

Error, because a forecast that is further from the truth is worse. Measured as
WAPE — total error over total demand — rather than MAPE, which divides by the
actual and therefore divides by zero in most weeks of most items.

Direction, because a forecast that is closer on average but consistently under
empties a shelf every week, and no measure of magnitude will ever show it. A
model can improve the headline number and cost more money, and this is the check
that catches it.
"""

from dataclasses import dataclass, field
from datetime import date
from math import sqrt

import pandas as pd

from forecast.calibration import Calibration
from forecast.calibration import fit as fit_calibration
from forecast.features import TARGET, Frame
from forecast.model import Model, train

# How far the published forecast may lean, as a share of demand, before it is
# refused however good its error rate. Not zero: no estimator is exactly unbiased
# on a finite sample, and demanding it would reject everything forever.
MAX_BIAS = 0.10


@dataclass
class Score:
    n: int
    mae: float | None = None
    mse: float | None = None
    rmse: float | None = None
    wape: float | None = None
    bias: float | None = None
    bias_pct: float | None = None

    def as_dict(self) -> dict:
        def r(v, p=3):
            return None if v is None else round(float(v), p)
        return {"n": self.n, "mae": r(self.mae, 2), "mse": r(self.mse, 2),
                "rmse": r(self.rmse, 2), "wape": r(self.wape, 4),
                "bias": r(self.bias, 2), "bias_pct": r(self.bias_pct, 4)}


def score(actual: list[float], predicted: list[float]) -> Score:
    """An empty comparison scores nothing rather than zero. A model with no
    evidence behind it is unmeasured, not perfect, and the two must never render
    the same on a screen somebody buys stock from."""
    if not actual:
        return Score(n=0)
    errors = [p - a for a, p in zip(actual, predicted, strict=True)]
    total = sum(actual)
    mae = sum(abs(e) for e in errors) / len(errors)
    mse = sum(e * e for e in errors) / len(errors)
    return Score(
        n=len(errors),
        mae=mae,
        mse=mse,
        rmse=sqrt(mse),
        wape=(sum(abs(e) for e in errors) / total) if total > 0 else None,
        bias=sum(errors) / len(errors),
        bias_pct=(sum(errors) / total) if total > 0 else None,
    )


def trailing_average(
    history: pd.DataFrame, *, sku: str, before: date, window: int = 8
) -> float:
    """The estimator in production, reproduced — including its stockout correction.

    Units over the trailing window divided by the weeks the item was actually
    sellable in it. Reproducing a weaker version of the incumbent is the most
    comfortable way to make a new model look good, so this follows the same rule
    the live one does: correct where the ledger covers the period, and fall back
    to the width of the window where it does not rather than inventing an
    availability nobody recorded.
    """
    rows = history[(history["sku"] == sku) & (history["week"] < before)]
    rows = rows.sort_values("week").tail(window)
    if rows.empty:
        return 0.0
    units = float(rows["item_units"].sum())
    days = rows["sellable_days"]
    # Corrected where the ledger covers the period; the width of the window where
    # it does not, rather than inventing an availability nobody recorded.
    weeks = (
        max(float(days.sum()) / 7.0, 0.5) if days.notna().all() else float(len(rows))
    )
    return units / weeks if weeks else 0.0



def decide(model_score: Score, baseline_score: Score) -> tuple[bool, list[str]]:
    """Does this model replace the incumbent? Two conditions, both required.

    Error, because a forecast further from the truth is worse.

    Direction, because a forecast closer on average but consistently under empties
    a shelf every week and no measure of magnitude shows it. The direction test is
    relative as well as absolute, and the relative half matters more than it looks:
    a flat ceiling sounds strict and is perverse where the incumbent is worse than
    the ceiling. Refusing a model that leans *less* than the estimator it would
    replace keeps the more one-sided forecast running, in the name of not
    publishing a one-sided one. So the model must be inside the ceiling, or at
    least no more one-sided than what is already in production.
    """
    if model_score.wape is None or baseline_score.wape is None:
        return False, ["nothing sold in the weeks held back, so there is no error "
                       "rate to compare"]

    reasons: list[str] = []
    beats = model_score.wape < baseline_score.wape
    if not beats:
        # A dead heat and a defeat are different findings, and the first one is
        # common: where the blend concludes the model adds nothing, what would be
        # published *is* the trailing average, and saying it was "wrong by 37.8%
        # against the average's 37.8%" describes one estimator as if it were two.
        if abs(model_score.wape - baseline_score.wape) < 1e-9:
            reasons.append(
                f"it is no better than the average it would replace — both wrong by "
                f"{model_score.wape:.1%} of demand — so there is nothing to gain by "
                f"swapping them"
            )
        else:
            reasons.append(
                f"it is wrong by {model_score.wape:.1%} of demand against the average's "
                f"{baseline_score.wape:.1%}"
            )

    drift = abs(model_score.bias_pct or 0.0)
    incumbent_drift = abs(baseline_score.bias_pct or 0.0)
    direction_ok = drift <= max(MAX_BIAS, incumbent_drift)
    way = "over" if (model_score.bias_pct or 0) > 0 else "under"
    cost = "tie cash up in stock" if way == "over" else "empty shelves"
    if not direction_ok:
        reasons.append(
            f"it {way}-forecasts by {drift:.1%} of demand — worse than the average's "
            f"{incumbent_drift:.1%} and past the {MAX_BIAS:.0%} limit — which would "
            f"{cost} every week"
        )
    elif beats and drift > MAX_BIAS:
        # Allowed out, and the lean is still worth saying out loud on the record.
        # It is inherited from the history rather than introduced by the model, and
        # it will not fix itself.
        #
        # Only when it is actually going out. This note read "published, but note"
        # on runs the error test had already refused, which told a reader the
        # opposite of what happened at the top of the same sentence.
        against = (
            f"no more one-sided than the average's {incumbent_drift:.1%}"
            if drift >= incumbent_drift - 1e-9
            else f"less one-sided than the average's {incumbent_drift:.1%}"
        )
        reasons.append(
            f"published, but note it {way}-forecasts by {drift:.1%} of demand. It is "
            f"{against}, which is why it was allowed out, and both lean the same way — "
            f"that is a property of the history, not of the model."
        )
    return bool(beats and direction_ok), reasons


@dataclass
class Evaluation:
    """What the holdout said, and whether it was enough."""

    passes: bool
    reasons: list[str]
    model_score: Score
    baseline_score: Score
    test_weeks: list[date]
    train_rows: int
    per_item: dict[str, dict]
    refusal: str | None = None
    # The corrections fitted on the calibration weeks and applied to the test
    # weeks. Carried out of here so the same ones can be applied when the model
    # is served — a model scored with a correction and then served without it is
    # not the thing that was scored.
    calibration: Calibration = field(default_factory=Calibration)
    # What the model scored before the corrections, so the effect of calibrating
    # is visible rather than asserted.
    raw_score: Score | None = None

    def as_dict(self) -> dict:
        return {
            "passes": self.passes,
            "reasons": self.reasons,
            "refusal": self.refusal,
            "model": self.model_score.as_dict(),
            "baseline": self.baseline_score.as_dict(),
            "test_weeks": [w.isoformat() for w in self.test_weeks],
            "train_rows": self.train_rows,
            "per_item": self.per_item,
            "calibration": self.calibration.as_dict(),
            "raw_model": self.raw_score.as_dict() if self.raw_score else None,
        }


def item_weeks(rows, model, history) -> pd.DataFrame:
    """Predictions and actuals per item per week, with the incumbent beside them.

    Scored where the buying happens rather than per customer: a model can be
    indifferent about which customer buys and still be exactly right about how
    many to order, and it is the order that costs money.
    """
    part = rows.copy()
    part["predicted"] = model.expected(part)
    grouped = (
        part.groupby(["sku", "week"], observed=True)
        .agg(actual=(TARGET, "sum"), predicted=("predicted", "sum"))
        .reset_index()
    )
    grouped["baseline"] = [
        trailing_average(history, sku=row.sku, before=row.week)
        for row in grouped.itertuples()
    ]
    return grouped


def item_history(frame: Frame) -> pd.DataFrame:
    """One row per item per week of what actually sold, for the incumbent to read."""
    return (
        frame.data[frame.data["week"] <= frame.last_known_week]
        .groupby(["sku", "week"], observed=True)
        .agg(item_units=("item_units", "first"), sellable_days=("sellable_days", "first"))
        .reset_index()
    )


def holdout(
    frame: Frame, *, test_weeks: int = 4, calibrate_weeks: int = 2
) -> tuple[Model | None, Evaluation]:
    """Train, calibrate, score, decide — on three separate stretches of time.

    The split is by time and never at random. A random split trains on next month
    to predict last month, which cannot happen in production and reports an
    accuracy that will never be seen again.

    Three stretches rather than two, because the model needs correcting and the
    correction needs judging, and the same weeks cannot do both. It learns on the
    earliest, its lean and its blend with the incumbent are measured on the
    middle, and the gate scores the whole corrected thing on the last — which
    nothing has touched. Fitting the correction on the scoring weeks would be
    choosing the answer and then marking your own paper.
    """
    rows = frame.train
    weeks = sorted(rows["week"].unique())
    if len(weeks) < test_weeks + calibrate_weeks + 4:
        return None, Evaluation(
            passes=False,
            reasons=[],
            refusal=(
                f"{len(weeks)} weeks of usable history. Holding {test_weeks} back to "
                f"score on and {calibrate_weeks} to calibrate on leaves too few to "
                f"learn from, so nothing was trained."
            ),
            model_score=Score(n=0), baseline_score=Score(n=0),
            test_weeks=[], train_rows=len(rows), per_item={},
        )

    test_cut = weeks[-test_weeks]
    calibrate_cut = weeks[-(test_weeks + calibrate_weeks)]
    train_rows = rows[rows["week"] < calibrate_cut]
    calibrate_rows = rows[(rows["week"] >= calibrate_cut) & (rows["week"] < test_cut)]
    test_rows = rows[rows["week"] >= test_cut]

    model, refusal = train(train_rows, horizon=frame.horizon)
    if model is None:
        return None, Evaluation(
            passes=False, reasons=[], refusal=refusal,
            model_score=Score(n=0), baseline_score=Score(n=0),
            test_weeks=list(weeks[-test_weeks:]), train_rows=len(train_rows), per_item={},
        )

    history = item_history(frame)

    # Fitted on weeks the model did not train on and the gate will not score on.
    # A lean measured on the training weeks would be measuring how well it
    # memorised them.
    tuning = item_weeks(calibrate_rows, model, history)
    calibration = fit_calibration(
        tuning["actual"].tolist(), tuning["predicted"].tolist(), tuning["baseline"].tolist()
    )

    grouped = item_weeks(test_rows, model, history)
    grouped["corrected"] = [
        calibration.apply(row.predicted, row.baseline) for row in grouped.itertuples()
    ]

    raw_score = score(grouped["actual"].tolist(), grouped["predicted"].tolist())
    model_score = score(grouped["actual"].tolist(), grouped["corrected"].tolist())
    baseline_score = score(grouped["actual"].tolist(), grouped["baseline"].tolist())

    per_item = {}
    for sku, part in grouped.groupby("sku", observed=True):
        per_item[str(sku)] = {
            "model": score(part["actual"].tolist(), part["corrected"].tolist()).as_dict(),
            "baseline": score(part["actual"].tolist(), part["baseline"].tolist()).as_dict(),
            "sold": float(part["actual"].sum()),
        }

    passes, reasons = decide(model_score, baseline_score)
    if model_score.wape is None or baseline_score.wape is None:
        return model, Evaluation(
            passes=False, reasons=reasons,
            model_score=model_score, baseline_score=baseline_score,
            test_weeks=list(weeks[-test_weeks:]), train_rows=len(train_rows),
            per_item=per_item, calibration=calibration, raw_score=raw_score,
        )

    return model, Evaluation(
        passes=passes,
        reasons=reasons,
        model_score=model_score,
        baseline_score=baseline_score,
        test_weeks=list(weeks[-test_weeks:]),
        train_rows=len(train_rows),
        per_item=per_item,
        calibration=calibration,
        raw_score=raw_score,
    )
