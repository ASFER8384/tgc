"""Correcting what the model gets systematically wrong, without cheating.

The first run on real data was refused for exactly one reason: it ran nineteen
percent high, worse than the average it was trying to replace. That is not a bad
model, it is an uncalibrated one — a Tweedie regressor trained only on the weeks
something sold, on seven weeks of history, will lean high, and no amount of
retraining fixes a lean because the lean is not in the pattern, it is in the
level.

Two corrections, in order.

**The factor.** How much the model over- or under-shoots in total, as one number.
Applied multiplicatively, clipped, shrunk towards one by how little evidence
stands behind it, and estimated on weeks the model did not train on — a factor
fitted on the training weeks would measure how well it memorised them.

**The blend.** How much to trust the model against the trailing average that is
already running. Zero means the average, one means the model, and anything
between is a weighted mix. This is not hedging: on a short history the honest
answer is often "some of each", and forcing a choice between them throws away the
half that was right.

The thing that makes this legitimate rather than tuning until it passes is *where*
both are fitted. The history is cut three ways — train, calibrate, test. The model
learns on the first, the corrections are fitted on the second, and the gate scores
the whole calibrated thing on the third, which nothing has touched. Fitting either
correction on the test weeks would be choosing the answer and then marking your
own paper.
"""

from dataclasses import dataclass

# How far the correction may move the level. Beyond this the model is not leaning,
# it is wrong about something structural, and quietly doubling its output would
# hide that behind a plausible number.
MIN_FACTOR = 0.5
MAX_FACTOR = 2.0

# The mixes considered. Coarse on purpose: a finer grid on a dozen validation
# points is fitting noise to three decimal places.
WEIGHTS = (0.0, 0.25, 0.5, 0.75, 1.0)

# Below this there is not enough out-of-sample evidence to fit anything. The
# uncalibrated model is returned instead of a correction invented from four
# numbers.
MIN_POINTS = 6

# How much evidence a level correction needs before it is trusted in full.
#
# The first version applied the raw ratio from however many points there were,
# and on eight of them it read a lean of eight percent that was not there and
# scaled the whole forecast up by it — taking a run that was fifteen percent high
# to twenty-one, and past the limit. A ratio measured on eight numbers is mostly
# noise, so it is pulled towards one in proportion to how little there is behind
# it: at eight points about a third of the correction survives, at fifty about
# four fifths, and a genuine lean measured over a long fold still arrives nearly
# whole.
SHRINK_POINTS = 16


@dataclass(frozen=True)
class Calibration:
    """A level correction and a blend weight, both fitted out of sample."""

    factor: float = 1.0
    # Weight on the corrected model. The remainder goes to the trailing average.
    alpha: float = 1.0
    points: int = 0
    fitted: bool = False

    def apply(self, model: float, baseline: float) -> float:
        """One corrected prediction. Never negative — a negative sale is not a
        thing, and a blend of two positive numbers should not need saying, but a
        model prediction can arrive slightly below zero from a tree."""
        return max(0.0, self.alpha * (model * self.factor) + (1.0 - self.alpha) * baseline)

    def as_dict(self) -> dict:
        return {
            "factor": round(self.factor, 4),
            "alpha": round(self.alpha, 2),
            "points": self.points,
            "fitted": self.fitted,
            "reads_as": self._describe(),
        }

    def _describe(self) -> str:
        if not self.fitted:
            return "not calibrated — too few out-of-sample weeks to fit anything"
        if self.alpha == 0:
            return ("the trailing average alone; the model added nothing on the weeks "
                    "it was checked against")
        level = (
            "left alone" if 0.97 <= self.factor <= 1.03
            else f"scaled by {self.factor:.2f} to correct a lean"
        )
        if self.alpha == 1:
            return f"the model alone, {level}"
        return f"{self.alpha:.0%} model {level}, {1 - self.alpha:.0%} trailing average"


def _wape(actual: list[float], predicted: list[float]) -> float | None:
    total = sum(actual)
    if total <= 0:
        return None
    return sum(abs(p - a) for a, p in zip(actual, predicted, strict=True)) / total


def _lean(actual: list[float], predicted: list[float]) -> float | None:
    """How far the predictions run high or low overall, as a share of demand.

    The gate refuses on this separately from error, so a correction that trades
    one for the other has not helped — it has moved the failure.
    """
    total = sum(actual)
    if total <= 0:
        return None
    return (sum(predicted) - total) / total


def fit(
    actual: list[float], model: list[float], baseline: list[float]
) -> Calibration:
    """Estimate the level correction and the blend on held-out weeks.

    ``actual``, ``model`` and ``baseline`` are aligned lists of item-weeks the
    model was not trained on.

    Two rules keep the correction from becoming the problem. The level factor is
    shrunk towards one by how little evidence stands behind it, and no candidate
    is allowed to leave the forecast leaning further than the raw model already
    did. Doing nothing is always among the candidates and always satisfies both,
    so there is a floor: calibration can improve a run or leave it alone, and can
    no longer make it worse.
    """
    if len(actual) < MIN_POINTS:
        return Calibration(points=len(actual))

    model_total = sum(model)
    actual_total = sum(actual)
    if model_total > 0 and actual_total > 0:
        raw = min(max(actual_total / model_total, MIN_FACTOR), MAX_FACTOR)
        weight = len(actual) / (len(actual) + SHRINK_POINTS)
        factor = 1.0 + (raw - 1.0) * weight
    else:
        # Nothing to scale against. Left at one rather than at zero: a model that
        # predicted nothing on these weeks is a finding for the blend to deal
        # with, not something to be multiplied away.
        factor = 1.0

    # The lean the raw model already has on this fold. Nothing may exceed it —
    # a correction whose job is to remove a lean does not get to add one.
    raw_lean = _lean(actual, model)
    ceiling = abs(raw_lean) if raw_lean is not None else None

    best = Calibration(factor=1.0, alpha=1.0, points=len(actual), fitted=True)
    best_error = None

    # The uncorrected model is itself a candidate — factor one, weight one — and
    # it always clears the ceiling, so whatever wins is never worse than doing
    # nothing. Ascending in alpha with the untouched level tried first, and a
    # strict improvement needed to move: on a tie the lower weight and the smaller
    # correction hold, because a new model should have to earn each point of trust
    # rather than inherit it from a coin flip.
    for candidate_factor in (1.0, factor):
        corrected = [m * candidate_factor for m in model]
        for alpha in WEIGHTS:
            blended = [
                alpha * c + (1 - alpha) * b
                for c, b in zip(corrected, baseline, strict=True)
            ]
            error = _wape(actual, blended)
            if error is None:
                continue
            lean = _lean(actual, blended)
            if ceiling is not None and lean is not None and abs(lean) > ceiling + 1e-9:
                continue
            if best_error is None or error < best_error - 1e-9:
                best = Calibration(
                    factor=candidate_factor, alpha=alpha, points=len(actual), fitted=True
                )
                best_error = error

    return best
