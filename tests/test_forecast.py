"""The demand forecast.

Most of what is pinned here is not "does it predict well" — that is measured on
the real history by the gate, and it changes every week. What is pinned is the
set of rules that decide whether a prediction may be believed at all: that the
zeros are in the table, that a week nobody could buy is not learned from as a
week nobody wanted it, that a feature never sees a week the forecaster could not
have seen, and that nothing reaches the buying desk without beating what is
already running.
"""

from datetime import UTC, datetime, timedelta

import pytest

from cdp.models.event import Event
from cdp.models.person import Person
from forecast import features as feature_module
from forecast import panel as panel_module
from forecast.evaluate import Score, decide, holdout
from forecast.model import MIN_ROWS, train
from sca.models import Item, StockLevel, StockSnapshot, Supplier

# A Monday, so week buckets line up with what the assertions say.
NOW = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
SKU = "ALN-ABAYA-01"
OTHER = "RWS-LIP-TUBE"


async def _catalogue(session, skus=(SKU, OTHER)):
    supplier = Supplier(code="ALN", name="Aleena Atelier")
    session.add(supplier)
    await session.flush()
    for sku in skus:
        session.add(Item(sku=sku, name=f"{sku} item", supplier_id=supplier.id, brand="aleena"))
        session.add(StockSnapshot(sku=sku, on_hand=100, on_order=0))
    await session.flush()


async def _person(session, name="Noura"):
    person = Person(display_name=name)
    session.add(person)
    await session.flush()
    return person.id


async def _sale(session, person_id, *, weeks_ago: float, quantity: int,
                sku: str = SKU, anonymous: bool = False):
    session.add(Event(
        person_id=person_id,
        source="pos" if anonymous else "shopify",
        name="order_paid",
        occurred_at=NOW - timedelta(weeks=weeks_ago),
        payload={"line_items": [{"sku": sku, "quantity": quantity}], "anonymous": anonymous},
    ))
    await session.flush()


# ------------------------------------------------------------------- the panel


async def test_the_panel_has_a_row_for_every_week_including_the_empty_ones(session):
    """Those zeros are most of the table and they are the point of it. Built only
    from weeks that sold something, the panel says everybody always buys — and a
    model trained on it will say so too."""
    await _catalogue(session, skus=(SKU,))
    person = await _person(session)
    await _sale(session, person, weeks_ago=6, quantity=4)
    await _sale(session, person, weeks_ago=1, quantity=2)

    built = await panel_module.build(session, now=NOW, timezone="UTC")
    # Six weeks, ending at the last one that finished. The week in progress is
    # absent on purpose — see the test below.
    assert [r.units for r in built.items[SKU]] == [4, 0, 0, 0, 0, 2]


async def test_availability_is_unknown_rather_than_full_where_the_ledger_is_silent(session):
    """Assuming an item was in stock before anybody recorded it is the same
    mistake as assuming it was not, and it would be invisible afterwards."""
    await _catalogue(session, skus=(SKU,))
    person = await _person(session)
    await _sale(session, person, weeks_ago=3, quantity=1)

    built = await panel_module.build(session, now=NOW, timezone="UTC")
    assert all(row.sellable_days is None for row in built.items[SKU])


async def test_a_week_with_nothing_on_the_shelf_is_marked_unknowable(session):
    await _catalogue(session, skus=(SKU,))
    person = await _person(session)
    await _sale(session, person, weeks_ago=5, quantity=5)
    session.add(StockLevel(sku=SKU, on_hand=0, on_order=0,
                           recorded_at=NOW - timedelta(weeks=4)))
    await session.flush()

    built = await panel_module.build(session, now=NOW, timezone="UTC")
    covered = [r for r in built.items[SKU] if r.sellable_days is not None]
    assert covered and all(r.stocked_out and r.unknowable for r in covered)


async def test_the_week_in_progress_is_left_out(session):
    """The most expensive line in this module.

    A week that is not over arrives holding whatever has happened so far, so a
    Monday reads as a 90% collapse. That is not a rounding error — it is the last
    row of the table, and the lag features every model here is built on lean on
    the last row hardest. The model learns that demand has just fallen off a
    cliff and forecasts the cliff forward.

    On the real trading history this was the difference between forecasting 7
    units a week and 40, against an item that actually sells about 35.
    """
    await _catalogue(session, skus=(SKU,))
    person = await _person(session)
    await _sale(session, person, weeks_ago=1, quantity=20)
    # A sale in the week that has not finished yet.
    await _sale(session, person, weeks_ago=0, quantity=1)

    built = await panel_module.build(session, now=NOW, timezone="UTC")
    assert built.items[SKU][-1].units == 20, "the last row must be a finished week"
    assert 1 not in [r.units for r in built.items[SKU]]


async def test_a_walk_in_sale_is_demand_but_not_a_customer(session):
    """Dropping it would understate demand for exactly the items that sell for
    cash. Reading it as a customer would invent a shopper with a remarkable
    habit."""
    await _catalogue(session, skus=(SKU,))
    person = await _person(session)
    await _sale(session, person, weeks_ago=2, quantity=3, anonymous=True)

    built = await panel_module.build(session, now=NOW, timezone="UTC")
    assert built.items[SKU][-2].units == 3
    assert built.rows == []
    assert built.attributed_share == 0.0


# ---------------------------------------------------------------- the features


async def _busy_shop(session, *, weeks: int = 14, people: int = 6):
    """Enough trade for a model to be trainable, with a shape in it: one item
    sells steadily and the other builds."""
    await _catalogue(session)
    ids = [await _person(session, f"Customer {n}") for n in range(people)]
    for week in range(weeks, 0, -1):
        for n, person in enumerate(ids):
            if (week + n) % 2 == 0:
                await _sale(session, person, weeks_ago=week, quantity=2 + (n % 3), sku=SKU)
            if (week + n) % 3 == 0:
                await _sale(session, person, weeks_ago=week, quantity=1 + (weeks - week) // 4,
                            sku=OTHER)
    return ids


async def test_no_feature_can_see_the_week_being_predicted(session):
    """The rule the whole exercise rests on. A four-week forecast built from last
    week's sales measures a job nobody has to do and reports an accuracy that will
    never be seen again."""
    await _busy_shop(session)
    built = await panel_module.build(session, now=NOW, timezone="UTC")

    one = feature_module.build(built, horizon=1, future_weeks=2)
    four = feature_module.build(built, horizon=4, future_weeks=2)

    def series(frame):
        rows = frame.data[
            (frame.data["person_id"] == frame.data["person_id"].iloc[0])
            & (frame.data["sku"] == SKU)
        ].sort_values("week")
        return rows["units"].tolist(), rows["lag_1"].tolist()

    units, lag_at_1 = series(one)
    _, lag_at_4 = series(four)

    # At horizon 1 the most recent known week is the one before; at horizon 4 it
    # is four before. Neither ever equals the week being predicted.
    assert lag_at_1[5] == units[4]
    assert lag_at_4[5] == units[1]


async def test_a_week_that_could_not_be_bought_is_kept_out_of_training(session):
    """Sales in it are zero and demand is not. A model taught the first when the
    second was true will keep the shelf empty."""
    await _busy_shop(session)
    session.add(StockLevel(sku=SKU, on_hand=0, on_order=0,
                           recorded_at=NOW - timedelta(weeks=3)))
    await session.flush()

    built = await panel_module.build(session, now=NOW, timezone="UTC")
    frame = feature_module.build(built, horizon=1, future_weeks=2)

    excluded = frame.data[frame.data["unknowable"]]
    assert not excluded.empty
    assert frame.train["unknowable"].any() == False  # noqa: E712 — the point is the value


async def test_the_future_weeks_carry_no_outcome(session):
    """Left missing rather than zero, so a stray row can never be trained on as
    though nothing was bought in a week that has not happened."""
    await _busy_shop(session)
    built = await panel_module.build(session, now=NOW, timezone="UTC")
    frame = feature_module.build(built, horizon=1, future_weeks=3)

    assert len(frame.future_weeks) == 3
    assert frame.future["units"].isna().all()
    assert frame.train["units"].notna().all()


# ------------------------------------------------------------------- the model


def test_the_model_refuses_rather_than_returning_numbers_from_nothing():
    """An unusable model that answers anyway is worse than no model, because
    somebody will buy stock with it."""
    import pandas as pd

    thin = pd.DataFrame({"units": [0.0] * 10, "sku": ["A"] * 10})
    model, refusal = train(thin, horizon=1)
    assert model is None
    assert str(MIN_ROWS) in refusal


async def test_a_trained_model_predicts_and_never_predicts_a_negative_sale(session):
    await _busy_shop(session, weeks=16, people=10)
    built = await panel_module.build(session, now=NOW, timezone="UTC")
    frame = feature_module.build(built, horizon=1, future_weeks=4)

    model, refusal = train(frame.train, horizon=1)
    assert model is not None, refusal

    expected = model.expected(frame.future)
    assert len(expected) == len(frame.future)
    assert (expected >= 0).all()
    probability = model.probability(frame.future)
    assert ((probability >= 0) & (probability <= 1)).all()


# -------------------------------------------------------------------- the gate


def test_a_model_that_is_further_from_the_truth_does_not_ship():
    passes, reasons = decide(
        Score(n=8, wape=0.55, bias_pct=0.01), Score(n=8, wape=0.50, bias_pct=0.05)
    )
    assert passes is False
    assert "55.0%" in reasons[0]


def test_a_model_that_leans_harder_than_the_average_does_not_ship():
    """Accuracy is not the only thing that costs money. A forecast closer on
    average but consistently under empties a shelf every week."""
    passes, reasons = decide(
        Score(n=8, wape=0.40, bias_pct=-0.30), Score(n=8, wape=0.50, bias_pct=0.05)
    )
    assert passes is False
    assert "empty shelves" in reasons[0]


def test_a_model_less_one_sided_than_the_average_ships_and_says_so():
    """The perverse case a flat ceiling gets wrong: refusing a model that leans
    less than the estimator it replaces keeps the *more* biased forecast running,
    in the name of not publishing a biased one."""
    passes, reasons = decide(
        Score(n=8, wape=0.48, bias_pct=0.188), Score(n=8, wape=0.50, bias_pct=0.204)
    )
    assert passes is True
    assert reasons and "published, but note" in reasons[0]


def test_nothing_sold_in_the_holdout_is_not_a_pass():
    passes, reasons = decide(Score(n=4, wape=None), Score(n=4, wape=None))
    assert passes is False
    assert "no error rate" in reasons[0]


async def test_too_little_history_trains_nothing_and_says_why(session):
    await _catalogue(session, skus=(SKU,))
    person = await _person(session)
    await _sale(session, person, weeks_ago=2, quantity=1)

    built = await panel_module.build(session, now=NOW, timezone="UTC")
    frame = feature_module.build(built, horizon=1, future_weeks=2)
    model, evaluation = holdout(frame, test_weeks=4)

    assert model is None
    assert evaluation.passes is False
    assert "weeks of usable history" in evaluation.refusal


# --------------------------------------------------------------------- the API


async def test_readiness_reports_the_gaps(client, session):
    await _catalogue(session, skus=(SKU,))
    person = await _person(session)
    await _sale(session, person, weeks_ago=2, quantity=3)
    await _sale(session, person, weeks_ago=1, quantity=2, anonymous=True)
    await session.commit()

    out = (await client.get("/forecast/readiness")).json()
    assert out["skus"] == 1
    assert out["units"] == 5
    assert out["anonymous_units"] == 2
    assert out["attributed_share"] == pytest.approx(0.6)
    assert out["availability_known_share"] == 0.0


async def test_a_refused_run_publishes_nothing_and_is_still_recorded(client, session):
    """A bad month for the model is a quiet month for everybody else: the buying
    desk keeps the figure it had, and the run is kept as evidence."""
    await _catalogue(session, skus=(SKU,))
    person = await _person(session)
    await _sale(session, person, weeks_ago=2, quantity=3)
    await session.commit()

    out = (await client.post("/forecast/run", json={})).json()
    assert out["passed"] is False
    assert out["published"] is False
    assert out["refusal"]

    runs = (await client.get("/forecast/runs")).json()
    assert len(runs) == 1 and runs[0]["passed"] is False

    snapshot = await session.get(StockSnapshot, SKU)
    await session.refresh(snapshot)
    assert float(snapshot.weekly_forecast) == 0.0


async def test_a_full_run_forecasts_items_and_names_who_will_buy(client, session):
    await _busy_shop(session, weeks=16, people=8)
    await session.commit()

    out = (await client.post(
        "/forecast/run", json={"horizon": 1, "weeks_ahead": 4, "test_weeks": 3}
    )).json()
    assert out["refusal"] is None or out["passed"] is False

    summary = (await client.get("/forecast/summary")).json()
    assert summary["state"] in {"published", "passed", "rejected"}

    if not out["passed"]:
        # Legitimate — on generated history the average is sometimes simply
        # better, and the gate is doing its job. Nothing further to assert.
        return

    assert summary["items"], "a published run must have items behind it"
    first = summary["items"][0]
    assert first["next_month"] >= 0
    assert first["weeks_cover"] is None or first["weeks_cover"] >= 0

    buyers = (await client.get(f"/forecast/buyers/{first['sku']}")).json()
    assert buyers["buyers"], "a published run should name candidate buyers"
    assert all(0.0 <= b["probability"] <= 1.0 for b in buyers["buyers"])
    # The forecast is published where the buying desk already reads it.
    snapshot = await session.get(StockSnapshot, first["sku"])
    await session.refresh(snapshot)
    assert float(snapshot.weekly_forecast) > 0


# ------------------------------------------------------------- the calibration


def test_the_level_correction_is_clipped_rather_than_unbounded():
    """Beyond the clip the model is not leaning, it is wrong about something
    structural, and quietly multiplying its output would hide that behind a
    plausible number."""
    from forecast.calibration import MAX_FACTOR, MIN_FACTOR, fit

    # The model predicted a tenth of what sold, for many weeks running. The raw
    # ratio is ten; nothing near it may reach the forecast.
    cal = fit([100.0] * 40, [10.0] * 40, [90.0] * 40)
    assert MIN_FACTOR <= cal.factor <= MAX_FACTOR

    # And the other way, on a model that predicted ten times too much.
    cal = fit([10.0] * 40, [100.0] * 40, [12.0] * 40)
    assert MIN_FACTOR <= cal.factor <= MAX_FACTOR


def test_a_model_that_runs_high_is_scaled_back_towards_the_truth():
    from forecast.calibration import SHRINK_POINTS, fit

    # The average is wide of the mark here, so the corrected model has to win on
    # its merits rather than by a tie. Where both are equally good the tie-break
    # keeps the weight on the incumbent, which is a separate rule.
    #
    # Only part of the lean is corrected on eight points. The whole of it is a
    # ratio measured on eight numbers, and applying that in full is how a lean
    # that was not there gets scaled into the forecast.
    cal = fit([10.0] * 8, [12.0] * 8, [14.0] * 8)
    assert cal.fitted is True
    assert cal.alpha == 1.0
    share = 8 / (8 + SHRINK_POINTS)
    assert cal.factor == pytest.approx(1 + (10 / 12 - 1) * share, rel=1e-6)
    assert 10.0 < cal.apply(12.0, 14.0) < 12.0

    # On a long fold the same lean arrives nearly whole.
    long = fit([10.0] * 200, [12.0] * 200, [14.0] * 200)
    assert long.factor < cal.factor
    assert long.apply(12.0, 14.0) == pytest.approx(10.0, rel=0.15)


def test_the_correction_may_never_leave_the_forecast_leaning_further():
    """The run that prompted this: a factor fitted on eight points read an eight
    percent lean that was not there, scaled the whole forecast up by it, and took
    a run from fifteen percent high to twenty-one and past the gate's limit. A
    correction whose job is to remove a lean does not get to add one."""
    from forecast.calibration import fit

    # Under-forecast on the fold it is fitted on, so the raw ratio says "scale
    # up" — but doing nothing is always a candidate and always allowed, so
    # nothing that survives can lean further than the model already did.
    actual = [10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0]
    model = [9.0, 8.0, 9.0, 8.0, 9.0, 8.0, 9.0, 8.0]
    baseline = [11.0, 11.0, 11.0, 11.0, 11.0, 11.0, 11.0, 11.0]
    cal = fit(actual, model, baseline)

    raw_lean = (sum(model) - sum(actual)) / sum(actual)
    published = [cal.apply(m, b) for m, b in zip(model, baseline, strict=True)]
    lean = (sum(published) - sum(actual)) / sum(actual)
    assert abs(lean) <= abs(raw_lean) + 1e-9


def test_a_tie_leaves_the_weight_with_the_estimator_already_running():
    """A new model should have to earn each point of trust, not inherit it from
    a coin flip."""
    from forecast.calibration import fit

    cal = fit([10.0] * 8, [10.0] * 8, [10.0] * 8)
    assert cal.alpha == 0.0


def test_a_model_that_adds_nothing_is_blended_out_entirely():
    """Zero weight is a legitimate outcome, not a failure. Forcing a choice
    between the model and the average throws away the half that was right."""
    from forecast.calibration import fit

    actual = [10.0, 12.0, 8.0, 11.0, 9.0, 10.0, 12.0, 8.0]
    useless = [5.0, 20.0, 3.0, 18.0, 2.0, 19.0, 4.0, 17.0]
    good = actual[:]
    cal = fit(actual, useless, good)
    assert cal.alpha == 0.0
    assert cal.apply(999.0, 10.0) == pytest.approx(10.0)


def test_too_few_weeks_leaves_the_model_uncorrected_rather_than_guessing():
    from forecast.calibration import fit

    cal = fit([10.0, 12.0], [20.0, 24.0], [10.0, 12.0])
    assert cal.fitted is False
    assert cal.factor == 1.0 and cal.alpha == 1.0
    assert "not calibrated" in cal.as_dict()["reads_as"]


async def test_the_calibration_is_fitted_on_weeks_the_gate_never_scores(session):
    """The rule that keeps this from being tuning until it passes: train,
    calibrate and score are three separate stretches of time."""
    await _busy_shop(session, weeks=20, people=10)
    built = await panel_module.build(session, now=NOW, timezone="UTC")
    frame = feature_module.build(built, horizon=1, future_weeks=2)

    model, evaluation = holdout(frame, test_weeks=3, calibrate_weeks=2)
    assert model is not None, evaluation.refusal

    scored = set(evaluation.test_weeks)
    assert len(scored) == 3
    # The calibration saw points, and none of them are weeks the gate scored.
    assert evaluation.calibration.points > 0
    assert evaluation.raw_score is not None
    # Both figures exist, so the effect of correcting is visible rather than
    # asserted.
    assert evaluation.model_score.n == evaluation.raw_score.n


# ---------------------------------------------------------------- the guidance


async def test_guidance_warns_when_the_reorder_point_is_shorter_than_the_mill(
    client, session
):
    """The highest-value number on that page, and the one most likely to be
    wrong. Triggering at four weeks against a forty-two day mill means every
    order arrives after the shelf emptied, and no forecast improvement rescues
    it."""
    supplier = Supplier(code="SLOW", name="Guangzhou Mill", lead_time_days=42)
    session.add(supplier)
    await session.flush()
    session.add(Item(sku=SKU, name="Abaya", supplier_id=supplier.id,
                     reorder_cover_weeks=4))
    session.add(StockSnapshot(sku=SKU, on_hand=100, on_order=0))
    person = await _person(session)
    await _sale(session, person, weeks_ago=2, quantity=5)
    await session.commit()

    out = (await client.get("/forecast/guidance")).json()
    row = out["items"][SKU]
    assert row["lead_time_weeks"] == pytest.approx(6.0)
    assert row["suggested_reorder_weeks"] == 7.0
    assert "arrives after the shelf is empty" in row["reorder_warning"]


async def test_guidance_reports_a_lumpy_line_as_lumpy(client, session):
    """A cover target set on the average is wrong in both directions on a line
    that arrives in lumps, and nothing on the page said which lines those are."""
    await _catalogue(session, skus=(SKU,))
    person = await _person(session)
    for weeks, quantity in ((8, 40), (7, 0), (6, 0), (5, 1), (4, 60), (3, 0), (2, 0), (1, 2)):
        if quantity:
            await _sale(session, person, weeks_ago=weeks, quantity=quantity)
    await session.commit()

    out = (await client.get("/forecast/guidance")).json()
    assert out["items"][SKU]["steadiness"] == "lumpy"


async def test_guidance_names_the_window_that_would_have_scored_best(client, session):
    """Chosen by walking forward, each week predicted only from the weeks before
    it — a window picked by looking at the weeks it is judged on is not a choice,
    it is a fit."""
    await _catalogue(session, skus=(SKU,))
    person = await _person(session)
    for weeks in range(16, 0, -1):
        await _sale(session, person, weeks_ago=weeks, quantity=4)
    await session.commit()

    out = (await client.get("/forecast/guidance")).json()
    row = out["items"][SKU]
    assert row["best_window"] in (4, 8, 13, 26)
    assert row["best_window_error"] is not None
    assert row["weeks_history"] >= 8


async def test_guidance_says_nothing_it_cannot_know(client, session):
    """A brand-new line has no rate, no window and no verdict, and the page says
    so rather than showing a confident zero."""
    await _catalogue(session, skus=(SKU,))
    await session.commit()

    out = (await client.get("/forecast/guidance")).json()
    row = out["items"][SKU]
    assert row["weekly"] is None
    assert row["weeks_cover"] is None
    assert row["best_window"] is None
    assert row["steadiness"] == "unknown"
