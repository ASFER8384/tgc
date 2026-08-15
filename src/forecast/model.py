"""The model: two LightGBM heads over the same features.

One head asks whether this customer buys this item at all next week; the other
asks how many, given that she does. The forecast is the product.

Split, rather than one regressor, because the panel is mostly zeros. A single
model trained to minimise squared error on a column that is ninety-odd percent
zero learns that predicting nearly zero everywhere minimises its loss, and it is
right — it just cannot be used to buy anything. Splitting the question lets each
head be good at its own job, and it produces a probability, which is what the
"who will buy" answer is made of.

LightGBM rather than XGBoost for one concrete reason on this data: it splits on
categories natively, so the item is one column rather than one column per SKU.
The parameters below are set for a small catalogue with a short history, which is
what this is — leaf-wise growth will otherwise carve out a leaf for a single week
of a single item and call it a pattern.
"""

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from lightgbm import LGBMClassifier, LGBMRegressor

from forecast.features import CATEGORICAL, FEATURES, TARGET

# Below these the model is not trained at all. A refusal is a result: an
# unusable model that returns numbers anyway is worse than no model, because
# somebody will buy stock with them.
MIN_ROWS = 200
MIN_POSITIVE = 25


@dataclass
class Model:
    """A trained pair, plus what it was trained on."""

    classifier: LGBMClassifier
    regressor: LGBMRegressor
    horizon: int
    rows: int
    positives: int
    features: list[str] = field(default_factory=lambda: list(FEATURES))
    params: dict = field(default_factory=dict)

    def _matrix(self, frame: pd.DataFrame) -> pd.DataFrame:
        # Reindexed to the trained column order. LightGBM will predict from
        # columns in a different order without complaining and return numbers
        # that mean nothing, so the order is restated here rather than trusted.
        matrix = frame.reindex(columns=self.features)
        for column in CATEGORICAL:
            matrix[column] = matrix[column].astype("category")
        return matrix

    def probability(self, frame: pd.DataFrame) -> np.ndarray:
        """How likely each customer is to buy each item in the target week."""
        if frame.empty:
            return np.zeros(0)
        return self.classifier.predict_proba(self._matrix(frame))[:, 1]

    def quantity(self, frame: pd.DataFrame) -> np.ndarray:
        """How many, if she buys. Floored at zero — a negative sale is not a
        thing, and a tree asked to extrapolate will occasionally produce one."""
        if frame.empty:
            return np.zeros(0)
        return np.clip(self.regressor.predict(self._matrix(frame)), 0.0, None)

    def expected(self, frame: pd.DataFrame) -> np.ndarray:
        """Expected units: probability times quantity.

        This is the number that gets summed into demand. It is deliberately not
        rounded — a hundred customers each with a two percent chance of buying one
        abaya is two abayas, and rounding each of them to zero first would report
        that nobody wants it.
        """
        return self.probability(frame) * self.quantity(frame)


def _classifier_params(rows: int) -> dict:
    return {
        "objective": "binary",
        "n_estimators": 300,
        "learning_rate": 0.05,
        # The brake on leaf-wise growth. With a short history the default will
        # happily isolate one customer in one week and treat it as a rule.
        "min_child_samples": max(20, rows // 200),
        "num_leaves": 15,
        "max_depth": 6,
        "subsample": 0.9,
        "subsample_freq": 1,
        "colsample_bytree": 0.9,
        "reg_lambda": 1.0,
        "verbose": -1,
        "n_jobs": 1,
    }


def _regressor_params(rows: int) -> dict:
    return {
        # Tweedie rather than squared error: units are non-negative counts with a
        # lump at zero and a long tail, which is the distribution this objective
        # exists for. Squared error on it predicts negative sales and optimises
        # the wrong middle.
        "objective": "tweedie",
        "tweedie_variance_power": 1.3,
        "n_estimators": 300,
        "learning_rate": 0.05,
        "min_child_samples": max(10, rows // 200),
        "num_leaves": 15,
        "max_depth": 6,
        "subsample": 0.9,
        "subsample_freq": 1,
        "colsample_bytree": 0.9,
        "reg_lambda": 1.0,
        "verbose": -1,
        "n_jobs": 1,
    }


def train(rows: pd.DataFrame, *, horizon: int) -> tuple[Model | None, str | None]:
    """Fit both heads. Returns the model, or nothing and the reason why not."""
    usable = rows.dropna(subset=[TARGET])
    if len(usable) < MIN_ROWS:
        return None, (
            f"only {len(usable)} rows of history to learn from, and at least "
            f"{MIN_ROWS} are needed before a model means anything"
        )

    y = usable[TARGET].to_numpy(dtype=float)
    bought = (y > 0).astype(int)
    positives = int(bought.sum())
    if positives < MIN_POSITIVE:
        return None, (
            f"only {positives} customer-weeks with a purchase in them; at least "
            f"{MIN_POSITIVE} are needed to learn what a purchase looks like"
        )
    if positives == len(usable):
        return None, "every row is a purchase, so there is nothing to tell apart"

    matrix = usable.reindex(columns=FEATURES)
    for column in CATEGORICAL:
        matrix[column] = matrix[column].astype("category")

    clf_params = _classifier_params(len(usable))
    classifier = LGBMClassifier(**clf_params)
    classifier.fit(matrix, bought, categorical_feature=CATEGORICAL)

    # The quantity head sees only the weeks something was actually bought. Trained
    # on the zeros as well it would learn the same "predict nothing" habit the
    # split exists to avoid, and the probability would then be applied to it twice.
    positive_rows = matrix[bought == 1]
    reg_params = _regressor_params(positives)
    regressor = LGBMRegressor(**reg_params)
    regressor.fit(positive_rows, y[bought == 1], categorical_feature=CATEGORICAL)

    return Model(
        classifier=classifier,
        regressor=regressor,
        horizon=horizon,
        rows=len(usable),
        positives=positives,
        params={"classifier": clf_params, "regressor": reg_params},
    ), None
