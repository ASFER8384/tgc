"""Turn stock and forecast into buying suggestions.

Cover is the unit, not units on hand. "We have 400 pieces" means nothing without
knowing whether that is three weeks or three days, and cover is the number a
buyer already thinks in.
"""

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from sca.config import Settings, get_settings
from sca.models import Issue, Item, StockSnapshot, Supplier, SupplierItem
from sca.planning.demand import Demand, weekly_demand
from sca.scheduling.windows import WorkingHours, is_open


@dataclass
class Suggestion:
    sku: str
    description: str
    supplier_id: str
    # None where there is no demand figure to divide by. A line can still be
    # suggested there — a hard minimum in units does not need a forecast — and
    # reporting nought weeks of cover for an item nobody has bought yet would be
    # a measurement rather than the absence of one.
    weeks_cover: float | None
    suggest_quantity: int
    unit_cost: Decimal
    line_total: Decimal
    reason: str
    # Where the demand number came from. Carried all the way to the console
    # because a buyer approving spend is entitled to know whether the figure
    # behind it was typed by a colleague or measured from sales.
    forecast_source: str
    weekly_demand: float
    # The floor in units this line is held above, and whether it is under it.
    # Carried separately from the reason text because the console colours the
    # row on it: a line below a minimum somebody set by hand is not the same
    # kind of suggestion as one that merely ran low on cover.
    minimum: int = 0
    below_minimum: bool = False
    observed: Demand | None = None
    # Everyone who could make this line, best first, each priced at their own
    # minimum. The buyer picks; the first is only where the cursor starts.
    options: list[dict] = field(default_factory=list)
    # The cover thresholds this line was actually judged against, and where they
    # came from. A trigger that varies per line has to say why it varied, or it
    # reads as the number moving on its own.
    reorder_weeks: float = 0.0
    target_weeks: float = 0.0
    threshold_from: str = ""
    # The chosen supplier's minimum, and whether this order is under it. Shown
    # rather than obeyed: a buyer who needs 316 should see 316 and be told the
    # mill wants 2,000, not be handed 2,000 and left to work out why.
    supplier_moq: int = 0
    below_supplier_minimum: bool = False

    @property
    def alternatives(self) -> int:
        """Others besides the one chosen. Zero means single-sourced, which is
        worth seeing before it becomes a problem."""
        return max(0, len(self.options) - 1)

    def as_dict(self) -> dict:
        return {
            "sku": self.sku,
            "description": self.description,
            "supplier_id": self.supplier_id,
            "weeks_cover": round(self.weeks_cover, 1) if self.weeks_cover is not None else None,
            "suggest_quantity": self.suggest_quantity,
            "unit_cost": str(self.unit_cost),
            "line_total": str(self.line_total),
            "reason": self.reason,
            "forecast_source": self.forecast_source,
            "reorder_weeks": round(self.reorder_weeks, 1),
            "target_weeks": round(self.target_weeks, 1),
            "threshold_from": self.threshold_from,
            "supplier_moq": self.supplier_moq,
            "below_supplier_minimum": self.below_supplier_minimum,
            "weekly_demand": round(self.weekly_demand, 1),
            "minimum": self.minimum,
            "below_minimum": self.below_minimum,
            "observed": self.observed.as_dict() if self.observed else None,
            "alternatives": self.alternatives,
            "options": self.options,
        }


class PlanningService:
    def __init__(self, session: AsyncSession, *, settings: Settings | None = None):
        self.session = session
        # Passed in by the API, which resolves the console's overrides over the
        # environment. Falling back to the environment keeps this callable from
        # a script or a test without one.
        self.settings = settings or get_settings()

    async def suggest(self, *, now: datetime | None = None) -> list[Suggestion]:
        now = now or datetime.now(UTC)
        items = {i.sku: i for i in await self.session.scalars(select(Item))}
        stock = {s.sku: s for s in await self.session.scalars(select(StockSnapshot))}
        suppliers = {s.id: s for s in await self.session.scalars(select(Supplier))}
        observed = await weekly_demand(
            self.session,
            now=now,
            window_weeks=self.settings.demand_window_weeks,
            # Per item where one is set. A seasonal line measured over the same
            # six months as a carton is the wrong number, not a steadier one.
            windows={
                sku: float(item.demand_window_weeks)
                for sku, item in items.items()
                if item.demand_window_weeks is not None
            },
        )
        # Who can actually make each SKU, and on what terms. An item still names
        # a supplier, which is used when nobody has been linked to it yet — but
        # where links exist they win, because they are the ones a person chose
        # per SKU rather than a single column standing in for a whole catalogue.
        sourcing: dict[str, list[SupplierItem]] = {}
        for link in await self.session.scalars(
            select(SupplierItem).where(SupplierItem.active.is_(True))
        ):
            sourcing.setdefault(link.sku, []).append(link)
        # Their record, from exceptions this system already raised against them.
        trouble = dict(
            (
                await self.session.execute(
                    select(Issue.supplier_id, func.count(Issue.id))
                    .where(Issue.supplier_id.is_not(None))
                    .group_by(Issue.supplier_id)
                )
            ).all()
        )

        out: list[Suggestion] = []
        for sku, item in items.items():
            snapshot = stock.get(sku)
            if snapshot is None:
                continue
            options = [
                link for link in sourcing.get(sku, [])
                if (s := suppliers.get(link.supplier_id)) is not None and s.active
            ]
            supplier = suppliers.get(options[0].supplier_id if options else item.supplier_id)
            if supplier is None or not supplier.active:
                continue

            # A typed forecast wins. Someone who sets one knows something the
            # sales history does not — a launch, a campaign, a season that has
            # not happened yet — and silently overruling them with an average of
            # the past is exactly how a buyer stops trusting the tool. Measured
            # demand fills the silence instead of arguing with the statement.
            weekly = float(snapshot.weekly_forecast or 0)
            # Whoever wrote the rate says so. It used to be reported as "manual"
            # whatever wrote it, which made the forecast's own figure — the one
            # this desk runs on every morning — read as something a person had
            # entered by hand and could be found and asked about.
            source = snapshot.weekly_forecast_source or "stated"
            measured = observed.get(sku)
            if weekly <= 0 and measured and measured.weekly > 0:
                weekly, source = measured.weekly, "sales"

            available = snapshot.on_hand + snapshot.on_order

            # The floor in units, which needs no forecast to be true. The item's
            # own figure wins where one is set, including a deliberate zero:
            # somebody who says this line has no minimum is answering the
            # question, and the global default should not overrule them.
            floor = self._policy(item, "min_stock", "min_stock_default")
            floor = int(floor or 0)
            below_floor = floor > 0 and available < floor
            # This line's thresholds: its own where somebody set one, otherwise
            # the pair the forecast derived for it, otherwise the deployment's.
            reorder_weeks = float(self._policy(
                item, "reorder_cover_weeks", "reorder_cover_weeks",
                derived=snapshot.model_reorder_weeks))
            target_weeks = float(self._policy(
                item, "target_cover_weeks", "target_cover_weeks",
                derived=snapshot.model_target_weeks))
            # Where the pair came from, so a buyer looking at a line that
            # triggered at seven weeks can see it was the mill's clock and this
            # line's own spread rather than a number somebody chose.
            if item.reorder_cover_weeks is not None:
                threshold_from = "set on the item"
            elif snapshot.model_reorder_weeks is not None:
                threshold_from = snapshot.model_threshold_basis or "the forecast"
            else:
                threshold_from = "the deployment default"

            if weekly <= 0 and not below_floor:
                # No forecast, nothing sold, and no floor anybody set. Suggesting
                # something here would be inventing demand, which is how
                # automation loses trust.
                continue

            weeks_cover = available / weekly if weekly > 0 else None
            # Two quantities, whichever is larger. Cover answers "enough for how
            # long", the floor answers "never fewer than this", and an item that
            # trips both wants the bigger of the two — topping up to a fifty
            # piece minimum while the forecast says eight weeks needs four
            # hundred would leave it triggering again the following week.
            raw_quantity = max(
                target_weeks * weekly - available if weekly > 0 else 0.0,
                float(floor - available) if below_floor else 0.0,
                0.0,
            )

            # Whoever this line costs least with — priced for the quantity
            # actually needed, not per unit.
            #
            # Found the hard way on real data: a mill quoting 38 against another
            # at 42 looks cheaper until its minimum order is 1000 and the other
            # will take 400. That is 38,000 against 16,800 for silk nobody
            # needs. Unit price is the number suppliers negotiate on and the
            # wrong one to choose on.
            ranked = self._rank(options, suppliers, trouble, raw_quantity, now)
            if ranked:
                best = ranked[0]
                supplier = suppliers[best["supplier_id"]]
                # Their price and their clock, but the quantity the line needs.
                # The ranking above is priced at each candidate's own minimum,
                # because that is what decides who is actually cheaper — but
                # winning that comparison is not a reason to order their minimum.
                pack = best.get("pack_size") or 1
                moq = best.get("moq") or 0
                quantity = self._round_need(raw_quantity, pack)
                unit_cost = Decimal(best["unit_cost"])
                # Their lead time for this SKU where they have quoted one: a mill
                # that turns packaging in ten days may take six weeks over a
                # woven abaya, and one figure for both orders the wrong one late.
                lead_days = best["lead_time_days"]
            else:
                pack, moq = item.pack_size, item.moq or 0
                quantity = self._round_need(raw_quantity, pack)
                unit_cost = Decimal(str(item.unit_cost))
                lead_days = supplier.lead_time_days
            if quantity <= 0:
                continue

            # Lead time is part of the trigger, not an afterthought: a mill that
            # takes six weeks must be ordered from before cover runs to four.
            #
            # The floor is checked separately and cannot be overruled by cover.
            # An item under its minimum is bought back up even where the
            # forecast says there is a season of stock in the building, because
            # the minimum is somebody stating a policy and the cover figure is
            # the system's estimate of one.
            trigger = max(reorder_weeks, lead_days / 7)
            if weeks_cover is not None and weeks_cover >= trigger and not below_floor:
                continue
            out.append(
                Suggestion(
                    sku=sku,
                    description=item.name,
                    supplier_id=supplier.id,
                    weeks_cover=weeks_cover,
                    suggest_quantity=quantity,
                    unit_cost=unit_cost,
                    line_total=(unit_cost * quantity).quantize(Decimal("0.01")),
                    options=ranked,
                    reason=self._reason(
                        weeks_cover=weeks_cover,
                        lead_days=lead_days,
                        source=source,
                        measured=measured,
                        available=available,
                        floor=floor,
                        below_floor=below_floor,
                    ),
                    forecast_source=source if weekly > 0 else "minimum",
                    reorder_weeks=reorder_weeks,
                    target_weeks=target_weeks,
                    threshold_from=threshold_from,
                    supplier_moq=moq,
                    below_supplier_minimum=bool(moq and quantity < moq),
                    weekly_demand=weekly,
                    minimum=floor,
                    below_minimum=below_floor,
                    observed=measured,
                )
            )
        # Lowest cover first. A line under a hard minimum with no demand figure
        # has no cover to sort by and leads instead: somebody stated a floor and
        # the shelf is under it, which needs no forecast to be worth acting on.
        out.sort(key=lambda s: s.weeks_cover if s.weeks_cover is not None else -1.0)
        return out

    def _policy(self, item, field: str, setting: str, derived=None):
        """This item's own figure, then the forecast's, then the deployment's.

        Null and not zero is what separates the first from the rest. A zero
        minimum is somebody saying this line has no floor; a null is somebody not
        having an opinion, and a deployment that later raises the default should
        move the second and leave the first alone.

        The forecast sits in the middle because it knows things the constant
        cannot: how long this mill actually takes, how unevenly this line sells,
        and how much one order is worth in weeks. A single figure for the whole
        catalogue is late for a slow supplier and early for a fast one, and it
        was late and early on the same screen. It stays the last resort for the
        lines the forecast cannot speak for — no lead time, or nothing sold yet.
        """
        value = getattr(item, field, None)
        if value is not None:
            return value
        if derived is not None:
            return derived
        return getattr(self.settings, setting)

    @staticmethod
    def _reason(
        *,
        weeks_cover: float | None,
        lead_days: int,
        source: str,
        measured: Demand | None,
        available: int,
        floor: int,
        below_floor: bool,
    ) -> str:
        """One line a buyer can check the suggestion against.

        The floor is stated first where it is the thing that fired, because it
        is the part somebody typed in and therefore the part they can argue
        with. Cover follows where there is one; on an item with no demand figure
        there is nothing honest to say about weeks, so nothing is said.
        """
        parts: list[str] = []
        if below_floor:
            parts.append(f"{available} available against a minimum of {floor}")
        if weeks_cover is not None:
            parts.append(
                f"{weeks_cover:.1f} weeks of cover against a {lead_days} day lead time"
            )
        else:
            parts.append(f"no forecast and nothing sold, {lead_days} day lead time")
        if source == "sales" and measured:
            detail = f"on {measured.units} sold in {measured.weeks:.0f} weeks"
            if measured.availability != "uncorrected":
                detail += " on sale"
            # The stockout is named rather than silently divided out, because it
            # is the part a buyer would query and the part that raised the figure.
            if measured.stockout_weeks >= 1:
                detail += f", {measured.stockout_weeks:.0f} weeks out of stock"
            parts.append(detail)
        return ", ".join(parts)

    @classmethod
    def _rank(cls, options, suppliers, trouble, raw_quantity, now) -> list[dict]:
        """Everyone who can make this line, best first, each priced their way.

        Three things are compared, in this order, because that is the order in
        which they cost the business money:

        1. What the line actually costs there. Each candidate is rounded up to
           their own minimum and pack size first — that is what turns a lower
           unit price into a larger bill, and it was found the hard way: a mill
           quoting 38 with a minimum of 1000 beats one quoting 42 on unit price
           and costs more than twice as much for silk nobody needed.
        2. How long they take, per SKU where they have quoted it.
        3. Their record — short shipments, slipped dates, silence. Deliberately
           called reliability and not quality: nothing here inspects the goods,
           and naming it quality would claim a measurement nobody takes.

        Whether they are open right now is carried but does not rank. It decides
        when a message gets read, not what the order costs, and a supplier who
        is merely awake should never outrank one who is cheaper.

        Currencies are not converted. Comparing across them needs a rate
        somebody owns, so this ranks within the majority currency and leaves the
        odd one out visible rather than inventing an exchange rate.
        """
        if not options:
            return []

        priced = []
        for link in options:
            supplier = suppliers[link.supplier_id]
            quantity = cls._round_up(raw_quantity, link.moq, link.pack_size)
            unit = Decimal(str(link.unit_cost))
            hours = WorkingHours.from_supplier(supplier)
            priced.append(
                {
                    "supplier_id": supplier.id,
                    "code": supplier.code,
                    "supplier": supplier.name,
                    "country": supplier.country,
                    "quantity": quantity,
                    "unit_cost": str(unit),
                    "currency": link.currency,
                    "line_total": str((unit * quantity).quantize(Decimal("0.01"))),
                    "lead_time_days": link.lead_time_days or supplier.lead_time_days,
                    "lead_time_is_theirs": link.lead_time_days is not None,
                    "moq": link.moq,
                    "pack_size": link.pack_size,
                    # What the line actually needs on their pack size, beside the
                    # quantity priced at their minimum. The comparison above needs
                    # the minimum; the order does not.
                    "need_quantity": cls._round_need(raw_quantity, link.pack_size),
                    "open_now": is_open(hours, now),
                    "issues_raised": int(trouble.get(supplier.id, 0)),
                }
            )

        currencies = [row["currency"] for row in priced]
        majority = max(set(currencies), key=currencies.count)
        priced.sort(
            key=lambda row: (
                row["currency"] != majority,
                Decimal(row["line_total"]),
                row["lead_time_days"],
                row["issues_raised"],
            )
        )
        return priced

    @staticmethod
    def _round_need(raw: float, pack_size: int) -> int:
        """What the line actually needs, in whole packs.

        The supplier's minimum is deliberately not applied here. It is a fact
        about them, not about the demand, and folding it into the suggestion
        turned "you need 316" into "buy 2,000" with nothing on screen saying
        which number was the need — a year of stock proposed as though the
        forecast had asked for it.

        The minimum still decides who is cheapest, in ``_rank``, where it
        belongs: that comparison is about what the line costs from each of them.
        And it is carried onto the suggestion so the desk can say the order is
        under it. What it no longer does is silently become the order.
        """
        quantity = max(int(math.ceil(raw)), 0)
        pack = max(pack_size, 1)
        if pack > 1 and quantity > 0:
            quantity = int(math.ceil(quantity / pack) * pack)
        return quantity

    @staticmethod
    def _round_up(raw: float, moq: int, pack_size: int) -> int:
        """Respect the two constraints every real supplier has: a minimum order
        quantity and a pack size. Both belong to whoever is being bought from,
        not to the product — the same silk can be 300 minimum from one mill and
        1000 from another, and that difference often decides which is actually
        cheaper. Rounding down would produce orders the supplier rejects, which
        is worse than buying slightly too much."""
        quantity = max(int(math.ceil(raw)), moq if raw > 0 else 0)
        pack = max(pack_size, 1)
        if pack > 1:
            quantity = int(math.ceil(quantity / pack) * pack)
        return quantity
