"""Turn stock and forecast into buying suggestions.

Cover is the unit, not units on hand. "We have 400 pieces" means nothing without
knowing whether that is three weeks or three days, and cover is the number a
buyer already thinks in.
"""

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sca.config import get_settings
from sca.models import Item, StockSnapshot, Supplier
from sca.planning.demand import Demand, weekly_demand


@dataclass
class Suggestion:
    sku: str
    description: str
    supplier_id: str
    weeks_cover: float
    suggest_quantity: int
    unit_cost: Decimal
    line_total: Decimal
    reason: str
    # Where the demand number came from. Carried all the way to the console
    # because a buyer approving spend is entitled to know whether the figure
    # behind it was typed by a colleague or measured from sales.
    forecast_source: str
    weekly_demand: float
    observed: Demand | None = None

    def as_dict(self) -> dict:
        return {
            "sku": self.sku,
            "description": self.description,
            "supplier_id": self.supplier_id,
            "weeks_cover": round(self.weeks_cover, 1),
            "suggest_quantity": self.suggest_quantity,
            "unit_cost": str(self.unit_cost),
            "line_total": str(self.line_total),
            "reason": self.reason,
            "forecast_source": self.forecast_source,
            "weekly_demand": round(self.weekly_demand, 1),
            "observed": self.observed.as_dict() if self.observed else None,
        }


class PlanningService:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.settings = get_settings()

    async def suggest(self, *, now: datetime | None = None) -> list[Suggestion]:
        now = now or datetime.now(UTC)
        items = {i.sku: i for i in await self.session.scalars(select(Item))}
        stock = {s.sku: s for s in await self.session.scalars(select(StockSnapshot))}
        suppliers = {s.id: s for s in await self.session.scalars(select(Supplier))}
        observed = await weekly_demand(self.session, now=now)

        out: list[Suggestion] = []
        for sku, item in items.items():
            snapshot = stock.get(sku)
            if snapshot is None:
                continue
            supplier = suppliers.get(item.supplier_id)
            if supplier is None or not supplier.active:
                continue

            # A typed forecast wins. Someone who sets one knows something the
            # sales history does not — a launch, a campaign, a season that has
            # not happened yet — and silently overruling them with an average of
            # the past is exactly how a buyer stops trusting the tool. Measured
            # demand fills the silence instead of arguing with the statement.
            weekly = float(snapshot.weekly_forecast or 0)
            source = "manual"
            measured = observed.get(sku)
            if weekly <= 0 and measured and measured.weekly > 0:
                weekly, source = measured.weekly, "sales"

            available = snapshot.on_hand + snapshot.on_order
            if weekly <= 0:
                # No forecast and nothing sold means no opinion. Suggesting
                # anything here would be inventing demand, which is how
                # automation loses trust.
                continue

            weeks_cover = available / weekly
            # Lead time is part of the trigger, not an afterthought: a mill that
            # takes six weeks must be ordered from before cover runs to four.
            trigger = max(
                self.settings.reorder_cover_weeks, supplier.lead_time_days / 7
            )
            if weeks_cover >= trigger:
                continue

            target_units = self.settings.target_cover_weeks * weekly
            raw_quantity = max(0.0, target_units - available)
            quantity = self._round_up(raw_quantity, item)
            if quantity <= 0:
                continue

            unit_cost = Decimal(str(item.unit_cost))
            out.append(
                Suggestion(
                    sku=sku,
                    description=item.name,
                    supplier_id=item.supplier_id,
                    weeks_cover=weeks_cover,
                    suggest_quantity=quantity,
                    unit_cost=unit_cost,
                    line_total=(unit_cost * quantity).quantize(Decimal("0.01")),
                    reason=(
                        f"{weeks_cover:.1f} weeks of cover against a "
                        f"{supplier.lead_time_days} day lead time"
                        + (
                            f", on {measured.units} sold in "
                            f"{measured.weeks:.0f} weeks"
                            if source == "sales" and measured
                            else ""
                        )
                    ),
                    forecast_source=source,
                    weekly_demand=weekly,
                    observed=measured,
                )
            )
        out.sort(key=lambda s: s.weeks_cover)
        return out

    @staticmethod
    def _round_up(raw: float, item: Item) -> int:
        """Respect the two constraints every real supplier has: a minimum order
        quantity and a pack size. Rounding down would produce orders the supplier
        rejects, which is worse than buying slightly too much."""
        quantity = max(int(math.ceil(raw)), item.moq if raw > 0 else 0)
        pack = max(item.pack_size, 1)
        if pack > 1:
            quantity = int(math.ceil(quantity / pack) * pack)
        return quantity
