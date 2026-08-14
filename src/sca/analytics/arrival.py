"""If we order today, when does it arrive.

The answer is assembled from parts rather than produced as one number, because
the parts have very different standing. How long until the supplier reads the
order is arithmetic on their own working hours. How long they take to make it is
either measured from orders you have actually received or quoted by them and
never checked. How long a container takes to cross seven thousand kilometres is
an assumption calibrated against published lane times, and it is the largest and
least certain term in the sum.

Presenting those as a single date would let the weakest of them borrow the
authority of the strongest. So every leg carries a basis — measured, quoted,
calendar or assumed — and the console prints it.

Weather is deliberately not a term in the sum. A real forecast is free to fetch
and is fetched, but turning "storm at origin" into "four days late" needs a
model trained on delay data nobody here has. It is reported as a warning beside
the estimate and changes no number.
"""

from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sca.analytics.geo import country_of, distance_km, origin
from sca.config import Settings, get_settings
from sca.models import AuditLog, PurchaseOrder, Supplier
from sca.scheduling.windows import WorkingHours, hours_until_open, is_open

# Transit assumptions, per mode: a fixed handling allowance plus a rate over
# great-circle distance. Calibrated against published door-to-door lane times —
# Guangzhou to Riyadh comes out near 29 days by sea and 8 by air, Istanbul near
# 10 by road, all of which sit inside the ranges forwarders quote.
#
# These are the least certain numbers in this module and the most important to
# be able to correct, which is why they are here in one table rather than spread
# through the arithmetic.
MODES: dict[str, tuple[float, float]] = {
    "air": (4.0, 0.6),
    "sea": (10.0, 2.6),
    "road": (4.0, 2.6),
}

# What a country is normally shipped by when nobody has said otherwise. Land
# neighbours go by road; everything else defaults to sea, which is what a
# textile or packaging buyer at this scale actually uses.
# Below this, a completed order says more about how it was recorded than about
# how long the supplier took.
MIN_CREDIBLE_LEAD_DAYS = 1.0

DEFAULT_MODE: dict[str, str] = {
    "SA": "road", "AE": "road", "KW": "road", "BH": "road",
    "QA": "road", "OM": "road", "JO": "road", "EG": "road", "TR": "road",
}


@dataclass
class Leg:
    """One stage of the journey, and how much it deserves to be believed."""

    name: str
    days: float
    # measured  — computed from orders this business actually received
    # quoted    — the supplier's own figure, never checked against an arrival
    # calendar  — arithmetic on working hours and public holidays
    # assumed   — our transit model, the weakest term and the one to correct
    basis: str
    detail: str = ""


@dataclass
class Arrival:
    supplier: str
    country: str | None
    hub: str | None
    distance_km: float | None
    mode: str
    legs: list[Leg] = field(default_factory=list)
    arrives_on: date | None = None
    total_days: float = 0.0
    # Real forecast, never converted into days. Empty when nothing notable or
    # when the service could not be reached — which is said rather than hidden.
    weather: str | None = None
    orders_measured: int = 0

    def as_dict(self) -> dict:
        landed = self.arrives_on.isoformat() if self.arrives_on else None
        return asdict(self) | {"arrives_on": landed}


def _holiday_dates(country: str | None, start: date, end: date) -> list[tuple[date, str]]:
    """Public holidays at the origin between two dates.

    A hard dependency would be wrong for a figure that is an improvement rather
    than a requirement, so a missing library costs the holiday leg and nothing
    else.
    """
    if not country or end < start:
        return []
    try:
        import holidays as holidays_lib
    except ImportError:
        return []
    try:
        calendar = holidays_lib.country_holidays(
            country.upper(), years=range(start.year, end.year + 1)
        )
    except (KeyError, NotImplementedError):
        return []
    return sorted(
        (day, str(name)) for day, name in calendar.items() if start <= day <= end
    )


class ArrivalService:
    def __init__(self, session: AsyncSession, *, settings: Settings | None = None):
        self.session = session
        self.settings = settings or get_settings()

    async def estimate(
        self, supplier: Supplier, *, mode: str | None = None, now: datetime | None = None
    ) -> Arrival:
        now = now or datetime.now(UTC)
        country = country_of(supplier)
        place = origin(country)
        km = distance_km(country)
        chosen = mode or DEFAULT_MODE.get(country or "", "sea")

        out = Arrival(
            supplier=supplier.name,
            country=country,
            hub=place[0] if place else None,
            distance_km=km,
            mode=chosen,
        )

        # 1. Reaching them. An order approved after they have gone home does not
        # start being made tonight, and this is the one delay entirely within
        # our control — it is why sending is scheduled rather than immediate.
        hours = WorkingHours.from_supplier(supplier)
        waiting = hours_until_open(hours, now) / 24
        out.legs.append(
            Leg(
                name="Reaches them",
                days=round(waiting, 1),
                basis="calendar",
                detail="they are at their desks now" if is_open(hours, now)
                else f"their office opens in {hours_until_open(hours, now):.0f} hours",
            )
        )

        # 2. Making it. Measured where there is history, quoted where there is
        # not — and the difference is stated, because a number nobody has ever
        # checked should not look like one that has been.
        measured, count = await self._measured_lead(supplier)
        out.orders_measured = count
        if measured is not None:
            out.legs.append(
                Leg(
                    name="They make and ship it",
                    days=round(measured, 1),
                    basis="measured",
                    detail=f"average of {count} order(s) you have received"
                    + (
                        f" · they quote {supplier.lead_time_days}"
                        if abs(measured - supplier.lead_time_days) >= 1
                        else ""
                    ),
                )
            )
            production = measured
        else:
            production = float(supplier.lead_time_days)
            out.legs.append(
                Leg(
                    name="They make and ship it",
                    days=production,
                    basis="quoted",
                    detail="their own figure — no completed order to check it against",
                )
            )

        # 3. Public holidays at the origin, inside the making window. This is the
        # term that catches Chinese New Year, which shuts mills for a fortnight
        # and is knowable a year ahead.
        window_start = (now + timedelta(days=waiting)).date()
        window_end = window_start + timedelta(days=production)
        shut = _holiday_dates(country, window_start, window_end)
        if shut:
            names = ", ".join(dict.fromkeys(name for _, name in shut[:3]))
            out.legs.append(
                Leg(
                    name="Closed at origin",
                    days=float(len(shut)),
                    basis="calendar",
                    detail=f"{len(shut)} public holiday(s): {names}",
                )
            )

        # 4. Crossing the distance. The assumption, named as one.
        if km is not None:
            base, per_1000 = MODES.get(chosen, MODES["sea"])
            transit = base + per_1000 * (km / 1000)
            # Where the country was never entered it is inferred from their
            # timezone, which is usually right and occasionally not — a mill on
            # Asia/Shanghai could be anywhere that keeps those hours. The whole
            # distance rests on that guess, so the guess is stated rather than
            # presented as a known origin.
            guessed = not (supplier.country or "").strip()
            out.legs.append(
                Leg(
                    name=f"In transit by {chosen}",
                    days=round(transit, 1),
                    basis="assumed",
                    detail=f"{km:,.0f} km from {place[0] if place else 'origin'} — "
                    "estimated, not tracked"
                    + (" · country inferred from their timezone" if guessed else ""),
                )
            )
        else:
            out.legs.append(
                Leg(
                    name="In transit",
                    days=0.0,
                    basis="assumed",
                    detail="country not set on this supplier, so distance is unknown",
                )
            )

        # 5. Landing it. A flat allowance until enough receipts exist to measure
        # one, at which point this becomes measured like the production leg.
        out.legs.append(
            Leg(
                name="Customs and delivery",
                days=float(self.settings.customs_clearance_days),
                basis="assumed",
                detail="flat allowance — not yet measured from your own receipts",
            )
        )

        out.total_days = round(sum(leg.days for leg in out.legs), 1)
        out.arrives_on = (now + timedelta(days=out.total_days)).date()
        return out

    async def _measured_lead(self, supplier: Supplier) -> tuple[float | None, int]:
        """Days from sending an order to booking it in, from your own history.

        Receipt time comes from the audit log rather than a column on the order,
        because the audit log already records exactly when the goods were booked
        in and by whom. Deriving it there costs a join and adds no schema.
        """
        orders = list(
            await self.session.scalars(
                select(PurchaseOrder).where(
                    PurchaseOrder.supplier_id == supplier.id,
                    PurchaseOrder.status == "received",
                    PurchaseOrder.sent_at.is_not(None),
                )
            )
        )
        if not orders:
            return None, 0
        by_id = {o.id: o for o in orders}
        receipts = list(
            await self.session.scalars(
                select(AuditLog).where(
                    AuditLog.action == "order.receive",
                    AuditLog.entity_id.in_(list(by_id)),
                )
            )
        )
        spans: list[float] = []
        for row in receipts:
            order = by_id.get(row.entity_id)
            if order is None or order.sent_at is None:
                continue
            days = (row.created_at - order.sent_at).total_seconds() / 86400
            # Nothing crosses a border in under a day. A same-hour receipt is
            # somebody clicking through a demo, or a backfill, or a correction to
            # an order that had already arrived — and averaging those in reports
            # a supplier who ships instantly, which is worse than reporting
            # nothing, because it looks measured.
            if days >= MIN_CREDIBLE_LEAD_DAYS:
                spans.append(days)
        if not spans:
            return None, 0
        return sum(spans) / len(spans), len(spans)
