"""Turn stock and forecast into buying suggestions.

Cover is the unit, not units on hand. "We have 400 pieces" means nothing without
knowing whether that is three weeks or three days, and cover is the number a
buyer already thinks in.
"""

import math
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sca.config import get_settings
from sca.models import Item, StockSnapshot, Supplier


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
        }


class PlanningService:
    def __init__(self, session: AsyncSession):
        self.session = session
        self.settings = get_settings()

    async def suggest(self) -> list[Suggestion]:
        items = {i.sku: i for i in await self.session.scalars(select(Item))}
        stock = {s.sku: s for s in await self.session.scalars(select(StockSnapshot))}
        suppliers = {s.id: s for s in await self.session.scalars(select(Supplier))}

        out: list[Suggestion] = []
        for sku, item in items.items():
            snapshot = stock.get(sku)
            if snapshot is None:
                continue
            supplier = suppliers.get(item.supplier_id)
            if supplier is None or not supplier.active:
                continue

            weekly = float(snapshot.weekly_forecast or 0)
            available = snapshot.on_hand + snapshot.on_order
            if weekly <= 0:
                # No forecast means no opinion. Suggesting anything here would be
                # inventing demand, which is how automation loses trust.
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
                    ),
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
