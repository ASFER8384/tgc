"""Turning the panel into something a model can be trained on.

Every feature here is strictly backward-looking, and by more than it looks. A
forecast made four weeks out cannot use last week's sales, because four weeks out
last week had not happened either — so every lag is shifted by the horizon before
any window is taken over it. Getting this wrong does not produce an error; it
produces an excellent score for a job nobody has to do.

One feature is deliberately absent. Whether an item was in stock during the week
being predicted is enormously informative and completely unavailable at the
moment of prediction, because that week has not happened. Using it would train a
model that cannot be served. Past availability is used instead, and the current
week's availability is used only to decide which rows may be learned from at all.
"""

from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np
import pandas as pd

from forecast.panel import WEEK, Panel

# Feature columns, in one place, because the model artefact and the serving path
# have to agree on them exactly and a mismatch is silent — LightGBM will happily
# predict from columns in a different order and return numbers that mean nothing.
NUMERIC = [
    "lag_1", "lag_2", "lag_3", "lag_4",
    "roll_4", "roll_8", "roll_13",
    "weeks_since_bought",
    "person_units_4", "person_orders_4", "person_skus_4", "person_tenure",
    "item_units_1", "item_units_4", "item_units_8", "item_buyers_4",
    "item_availability_4",
    "week_sin", "week_cos", "month",
]
CATEGORICAL = ["sku"]
FEATURES = NUMERIC + CATEGORICAL
TARGET = "units"

# A ceiling on the customer by item by week grid. Every customer is paired with
# every item, which is what lets the model say who will buy something they have
# never bought — and it also means the table grows as the product of three
# numbers. Beyond this the least recently active customers are dropped first and
# the fact is reported, rather than the process quietly taking the machine down.
MAX_CELLS = 2_000_000


@dataclass
class Frame:
    data: pd.DataFrame
    horizon: int
    last_known_week: date
    future_weeks: list[date]
    people_dropped: int = 0

    @property
    def train(self) -> pd.DataFrame:
        """Rows whose outcome is both known and knowable.

        A week the item could not be sold is excluded rather than learned from as
        a zero: it is the difference between "nobody wanted it" and "we had none",
        and a model taught the first when the second was true will keep the shelf
        empty.
        """
        data = self.data
        return data[(data["week"] <= self.last_known_week) & (~data["unknowable"])]

    @property
    def future(self) -> pd.DataFrame:
        return self.data[self.data["week"] > self.last_known_week]


def _grid(panel: Panel, weeks: list[date]) -> tuple[pd.DataFrame, int]:
    """Every customer against every item, for every week. Including the zeros."""
    people = list(panel.people)
    skus = sorted(panel.items)
    if not people or not skus or not weeks:
        return pd.DataFrame(columns=["person_id", "sku", "week"]), 0

    dropped = 0
    per_person = len(skus) * len(weeks)
    if per_person and len(people) * per_person > MAX_CELLS:
        keep = max(1, MAX_CELLS // per_person)
        # Most recently active first — a customer who bought last month is more
        # informative about next month than one who bought once a year ago.
        last_seen: dict[str, date] = {}
        for row in panel.rows:
            if not row.units:
                continue
            if row.person_id not in last_seen or row.week > last_seen[row.person_id]:
                last_seen[row.person_id] = row.week
        people = sorted(people, key=lambda p: last_seen.get(p, date.min), reverse=True)[:keep]
        dropped = len(panel.people) - len(people)

    index = pd.MultiIndex.from_product(
        [people, skus, weeks], names=["person_id", "sku", "week"]
    )
    return pd.DataFrame(index=index).reset_index(), dropped


def build(panel: Panel, *, horizon: int = 1, future_weeks: int = 4) -> Frame:
    """The full feature table: history to train on, and the weeks still to come.

    Both are built together and by the same code. A serving path that assembles
    its features separately from the training path is the most common way a model
    that scored well offline behaves differently in production, and the failure is
    invisible — the numbers still look like numbers.
    """
    if panel.first_week is None or panel.last_week is None:
        empty = pd.DataFrame(columns=[*FEATURES, TARGET, "person_id", "week", "unknowable"])
        return Frame(data=empty, horizon=horizon, last_known_week=date.min, future_weeks=[])

    weeks = panel.week_list
    ahead = [panel.last_week + WEEK * (i + 1) for i in range(future_weeks)]
    grid, dropped = _grid(panel, weeks + ahead)
    if grid.empty:
        empty = pd.DataFrame(columns=[*FEATURES, TARGET, "person_id", "week", "unknowable"])
        return Frame(data=empty, horizon=horizon, last_known_week=panel.last_week,
                     future_weeks=ahead, people_dropped=dropped)

    sold = pd.DataFrame(
        [{"person_id": r.person_id, "sku": r.sku, "week": r.week, "units": r.units,
          "orders": r.orders} for r in panel.rows],
        columns=["person_id", "sku", "week", "units", "orders"],
    )
    data = grid.merge(sold, on=["person_id", "sku", "week"], how="left")
    data["units"] = data["units"].fillna(0.0)
    data["orders"] = data["orders"].fillna(0.0)
    # The future has no outcome. Left as missing rather than zero so a stray row
    # can never be trained on as though nothing was bought in a week that has not
    # happened.
    future_mask = data["week"] > panel.last_week
    data.loc[future_mask, ["units", "orders"]] = np.nan

    item_rows = pd.DataFrame(
        [{"sku": row.sku, "week": row.week, "item_units": row.units,
          "item_buyers": row.buyers, "sellable_days": row.sellable_days}
         for rows in panel.items.values() for row in rows],
        columns=["sku", "week", "item_units", "item_buyers", "sellable_days"],
    )
    data = data.merge(item_rows, on=["sku", "week"], how="left")
    data["item_units"] = data["item_units"].fillna(0.0)
    data["item_buyers"] = data["item_buyers"].fillna(0.0)

    # Which rows must not be learned from: the item sold nothing and is known to
    # have had nothing to sell. Unknown availability is not the same thing and is
    # left in — silence in the ledger is not evidence of an empty shelf.
    data["unknowable"] = (
        (data["units"].fillna(0) == 0)
        & data["sellable_days"].notna()
        & (data["sellable_days"] <= 0)
    )

    data = data.sort_values(["person_id", "sku", "week"], kind="stable").reset_index(drop=True)
    pair = data.groupby(["person_id", "sku"], observed=True)["units"]

    # The shift that makes the whole thing honest. `horizon` weeks of the most
    # recent history are withheld, because at the moment of forecasting they had
    # not happened either.
    base = pair.shift(horizon)
    data["lag_1"] = base
    data["lag_2"] = pair.shift(horizon + 1)
    data["lag_3"] = pair.shift(horizon + 2)
    data["lag_4"] = pair.shift(horizon + 3)
    for window in (4, 8, 13):
        data[f"roll_{window}"] = (
            base.groupby([data["person_id"], data["sku"]], observed=True)
            .transform(lambda s, w=window: s.rolling(w, min_periods=1).mean())
        )

    bought = (base.fillna(0) > 0).astype(int)
    # Weeks since this customer last bought this item, as known at the origin.
    # Counted from a running marker rather than a window, so an item somebody
    # bought once a year ago is distinguishable from one they never bought.
    seq = data.groupby(["person_id", "sku"], observed=True).cumcount()
    last_buy = seq.where(bought == 1)
    last_buy = last_buy.groupby([data["person_id"], data["sku"]], observed=True).ffill()
    data["weeks_since_bought"] = (seq - last_buy).astype("float64")

    # What this customer has been doing lately, across everything.
    person_week = (
        data[data["week"] <= panel.last_week]
        .groupby(["person_id", "week"], observed=True)
        .agg(units=("units", "sum"), orders=("orders", "sum"),
             skus=("units", lambda s: int((s > 0).sum())))
        .reset_index()
    )
    person_week = person_week.sort_values(["person_id", "week"], kind="stable")
    for name, column in (("person_units_4", "units"), ("person_orders_4", "orders"),
                         ("person_skus_4", "skus")):
        person_week[name] = (
            person_week.groupby("person_id", observed=True)[column]
            .transform(lambda s: s.shift(horizon).rolling(4, min_periods=1).mean())
        )
    data = data.merge(
        person_week[["person_id", "week", "person_units_4", "person_orders_4", "person_skus_4"]],
        on=["person_id", "week"], how="left",
    )
    # Forward-filled into the future weeks: what she was doing at the origin is
    # the best statement available about her, and it does not change because the
    # week being predicted has not happened.
    data = data.sort_values(["person_id", "sku", "week"], kind="stable")
    for name in ("person_units_4", "person_orders_4", "person_skus_4"):
        data[name] = data.groupby(["person_id", "sku"], observed=True)[name].ffill()

    first_seen = (
        sold[sold["units"] > 0].groupby("person_id")["week"].min()
        if not sold.empty else pd.Series(dtype="object")
    )
    tenure_days = data["person_id"].map(first_seen)
    data["person_tenure"] = [
        np.nan if pd.isna(seen) else (week - seen).days / 7.0
        for week, seen in zip(data["week"], tenure_days, strict=True)
    ]

    # And what the item has been doing, across everybody. This is the feature that
    # carries a customer with almost no history of her own: what she will buy is
    # mostly what is selling.
    item_week = item_rows.sort_values(["sku", "week"], kind="stable").copy()
    grouped = item_week.groupby("sku", observed=True)
    item_week["item_units_1"] = grouped["item_units"].shift(horizon)
    for window in (4, 8):
        item_week[f"item_units_{window}"] = grouped["item_units"].transform(
            lambda s, w=window: s.shift(horizon).rolling(w, min_periods=1).mean()
        )
    item_week["item_buyers_4"] = grouped["item_buyers"].transform(
        lambda s: s.shift(horizon).rolling(4, min_periods=1).mean()
    )
    # Past availability, which *is* known at the origin — unlike the availability
    # of the week being predicted, which is not and is therefore not a feature.
    item_week["item_availability_4"] = grouped["sellable_days"].transform(
        lambda s: s.shift(horizon).rolling(4, min_periods=1).mean()
    )
    keep = ["sku", "week", "item_units_1", "item_units_4", "item_units_8",
            "item_buyers_4", "item_availability_4"]
    data = data.merge(item_week[keep], on=["sku", "week"], how="left")
    data = data.sort_values(["person_id", "sku", "week"], kind="stable")
    for name in keep[2:]:
        data[name] = data.groupby(["person_id", "sku"], observed=True)[name].ffill()

    weeks_index = pd.to_datetime(data["week"])
    week_number = weeks_index.dt.isocalendar().week.astype(float)
    data["week_sin"] = np.sin(2 * np.pi * week_number / 52.0)
    data["week_cos"] = np.cos(2 * np.pi * week_number / 52.0)
    data["month"] = weeks_index.dt.month.astype(float)

    # Passed to LightGBM as a category rather than one-hot: it splits on groups of
    # items in a single cut, and two hundred near-empty indicator columns is how a
    # catalogue turns a usable model into an unusable one.
    data["sku"] = data["sku"].astype("category")

    return Frame(
        data=data.reset_index(drop=True),
        horizon=horizon,
        last_known_week=panel.last_week,
        future_weeks=ahead,
        people_dropped=dropped,
    )


def next_weeks(last: date, count: int) -> list[date]:
    return [last + timedelta(weeks=i + 1) for i in range(count)]
