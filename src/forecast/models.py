"""What was predicted, by which model, and whether it was allowed out.

Predictions are stored rather than computed on demand for one reason: a forecast
that is overwritten by the next one can never be checked against what actually
happened. Keeping them is what makes it possible to say, in three months, that
the model was running eight percent low all spring — and that sentence is worth
more than any figure produced on the day.

Rejected runs are kept alongside accepted ones. Keeping only the winners hides
the trend that says the data has changed.
"""

from datetime import date, datetime

from sqlalchemy import Boolean, Date, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from sca.models.base import Base, JSONType, TimestampMixin, UTCDateTime, new_id


class ForecastRun(Base, TimestampMixin):
    """One training and scoring attempt, passed or failed."""

    __tablename__ = "forecast_runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    ran_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    actor: Mapped[str] = mapped_column(String(64), nullable=False, default="system")
    horizon_weeks: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    weeks_history: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    train_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Whether it cleared the gate. A run that did not is still a record — the
    # metrics on it are the evidence for what to fix.
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Whether its numbers were written where the buying desk can act on them.
    # Separate from `passed` because a run can clear the gate and still be held
    # back by a person.
    published: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Why not, in a sentence somebody can act on. "It did not ship" is not an
    # answer anybody can do anything with.
    refusal: Mapped[str | None] = mapped_column(Text)
    metrics: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    params: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)


class ForecastItem(Base):
    """Predicted units for one item in one week."""

    __tablename__ = "forecast_items"
    __table_args__ = (Index("ix_forecast_items_run_sku", "run_id", "sku"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("forecast_runs.id"), nullable=False, index=True
    )
    sku: Mapped[str] = mapped_column(String(64), nullable=False)
    week: Mapped[date] = mapped_column(Date, nullable=False)
    units: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    # How many distinct customers are expected to buy it — the sum of the
    # probabilities, which is what an expected count is. Carried because "twelve
    # units to four people" and "twelve units to one person" are different
    # businesses and the same forecast.
    buyers: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)


class ForecastBuyer(Base):
    """One customer's likelihood of buying one item in the coming weeks.

    Only the ranked head of the list is stored. Every customer against every item
    is a row count nobody reads and a privacy surface nobody needs.
    """

    __tablename__ = "forecast_buyers"
    __table_args__ = (Index("ix_forecast_buyers_run_sku", "run_id", "sku"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("forecast_runs.id"), nullable=False, index=True
    )
    sku: Mapped[str] = mapped_column(String(64), nullable=False)
    person_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    probability: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    expected_units: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
