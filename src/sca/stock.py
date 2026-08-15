"""One shelf, whoever sold from it.

Stock lived in two places that never spoke. Shopify decremented its own count on
every online order; this platform decremented ``StockSnapshot`` only when a sale
was rung up at a counter. Neither knew about the other, so the two numbers began
drifting apart at the first online sale and never converged again.

The platform is the one that has to own the number, because it is the only place
that sees all three channels — the storefront only ever knows its own. So every
sale, from wherever, moves the shelf here, and here is what gets pushed back out.

The ledger append is the point of the whole exercise rather than a side effect:
demand is measured over the weeks an item was *sellable*, read from these rows. A
sale that moved stock without leaving a reading behind would be counted as demand
while contributing nothing to the divisor it belongs to.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sca.models import Item, StockAtLocation, StockAtVariant, StockLevel, StockSnapshot

# Where a sale came off when nobody said. The storefront, because that is the
# shelf whose count can be checked against Shopify in a second — a wrong guess
# there is visible immediately, where a wrong guess in a shop is discovered by a
# customer standing in front of an empty rail.
DEFAULT_LOCATION = "online"


@dataclass
class Movement:
    sku: str
    sold: int
    was: int
    now: int
    location: str = DEFAULT_LOCATION
    # Set when the shelf did not have what the sale says left it. The record was
    # wrong before the sale, not because of it.
    shortfall: str | None = None

    def as_dict(self) -> dict:
        # The shortfall is deliberately not in here. It travels in the response's
        # notes, and a line that carried its own copy would report the same
        # discrepancy twice to whoever is trying to reconcile one shelf.
        return {"sku": self.sku, "sold": self.sold, "was": self.was, "now": self.now}


async def sell(
    session: AsyncSession,
    quantities: dict[str, int],
    *,
    occurred: datetime,
    location: str = DEFAULT_LOCATION,
) -> list[Movement]:
    """Take sold units off one shelf, and keep the group's total in step.

    ``quantities`` is SKU to units, already merged: two lines of the same item in
    one basket must be added up before they arrive, or the arithmetic below reads
    twice from a position it has already changed.

    Two records move together and neither is derived later. The shelf, because a
    customer standing in the Jeddah shop cannot buy what is in Riyadh; and the
    total, because an order goes to a mill for the group. Recomputing the total
    from the shelves on every read would be tidier and would also mean the buying
    desk could not be looked at while a stocktake was half-entered.
    """
    out: list[Movement] = []
    for sku, quantity in sorted(quantities.items()):
        if quantity <= 0:
            continue
        snapshot = await session.get(StockSnapshot, sku)
        if snapshot is None:
            snapshot = StockSnapshot(sku=sku)
            session.add(snapshot)
            await session.flush()
        shelf = await session.get(StockAtLocation, (sku, location))
        if shelf is None:
            # No row for this shelf. Two very different situations, and reading
            # both as "the shelf is empty" was wrong in the second.
            #
            # If the item has *no* breakdown at all, nobody has split it yet and
            # the whole total is wherever it is being sold from — which is how a
            # single-location shop behaves, and how every deployment behaved
            # before locations existed.
            #
            # If it has a breakdown that this location is not part of, the shelf
            # is genuinely empty and the sale is short.
            split = (
                await session.scalars(
                    select(StockAtLocation).where(StockAtLocation.sku == sku)
                )
            ).all()
            shelf = StockAtLocation(
                sku=sku,
                location_code=location,
                on_hand=0 if split else snapshot.on_hand,
                on_order=0 if split else snapshot.on_order,
            )
            session.add(shelf)
            await session.flush()

        # The shelf first, and the total moves by what actually left it. If the
        # shelf held less than the sale says, the group is short by what was
        # really taken rather than by what was asked for — otherwise a bad count
        # in one shop would quietly write down the stock in the other.
        taken = min(quantity, shelf.on_hand) if shelf.on_hand > 0 else 0
        shelf.on_hand = max(0, shelf.on_hand - quantity)

        before = snapshot.on_hand
        after = before - (taken or quantity)
        shortfall = None
        if quantity > taken:
            shortfall = (
                f"{sku}: sold {quantity} at {location} but only {taken} were on that "
                "shelf. Held at 0 — the count needs checking."
            )
            after = before - taken
        if after < 0:
            # Held at zero rather than allowed to go negative. A negative on-hand
            # reads downstream as "not sellable", which shrinks the divisor in the
            # demand calculation and inflates the very figure this exists to
            # measure — the shelf would end up buying more of itself.
            after = 0
        snapshot.on_hand = after
        # Two readings: the group's, which is what demand is divided by and what
        # every existing row in this ledger is, and the shelf's.
        session.add(
            StockLevel(sku=sku, on_hand=after, on_order=snapshot.on_order, recorded_at=occurred)
        )
        session.add(
            StockLevel(
                sku=sku, on_hand=shelf.on_hand, on_order=shelf.on_order,
                recorded_at=occurred, location_code=location,
            )
        )
        out.append(
            Movement(
                sku=sku, sold=quantity, was=before, now=after,
                location=location, shortfall=shortfall,
            )
        )
    return out


async def set_shelf(
    session: AsyncSession,
    sku: str,
    location: str,
    on_hand: int,
    *,
    occurred: datetime,
) -> tuple[int, int]:
    """Put a counted figure on one shelf and re-add the group's total.

    This is a count arriving, not a movement: somebody has walked the rail, or
    the storefront has told us what it holds. So the figure is written rather
    than subtracted from, and the group's total is re-added from the shelves
    afterwards rather than nudged by the difference — nudging would carry any
    error in the old figure forward forever.

    Returns the shelf's figure before and after, so a caller can report what
    actually moved instead of what it asked for.
    """
    on_hand = max(0, on_hand)
    shelf = await session.get(StockAtLocation, (sku, location))
    if shelf is None:
        shelf = StockAtLocation(sku=sku, location_code=location)
        session.add(shelf)
        await session.flush()
    before = shelf.on_hand
    if before == on_hand:
        return before, on_hand
    shelf.on_hand = on_hand
    session.add(
        StockLevel(
            sku=sku, on_hand=on_hand, on_order=shelf.on_order,
            recorded_at=occurred, location_code=location,
        )
    )
    await retotal(session, sku, occurred=occurred)
    return before, on_hand


async def set_variant(
    session: AsyncSession,
    sku: str,
    location: str,
    variant: str,
    on_hand: int,
    *,
    occurred: datetime,
) -> tuple[int, int]:
    """Count one size on one shelf, and re-add everything above it.

    Three levels, one rule: the group is the sum of its shelves, a shelf is the
    sum of the variants counted on it. So counting a Small in Jeddah re-adds
    Jeddah, and re-adding Jeddah re-adds the group.

    The consequence has to be said plainly, because it surprises people the
    first time: **the moment any size is counted on a shelf, that shelf's total
    becomes the sum of its sizes.** A shelf holding three abayas where somebody
    counts one Small and stops now reads as holding one. That is not a bug and
    it must not be softened by carrying the missing two as a remainder — a
    remainder would be a number nobody counted, sitting in a total everybody
    trusts, and it would never be corrected because nothing would flag it. If
    the shelf really holds three, the other two get counted too.

    Returns the variant's figure before and after.
    """
    on_hand = max(0, on_hand)
    variant = variant.strip()
    row = await session.get(StockAtVariant, (sku, location, variant))
    if row is None:
        row = StockAtVariant(sku=sku, location_code=location, variant=variant)
        session.add(row)
        await session.flush()
    before = row.on_hand
    row.on_hand = on_hand
    await session.flush()
    await refold(session, sku, location, occurred=occurred)
    return before, on_hand


async def refold(
    session: AsyncSession, sku: str, location: str, *, occurred: datetime
) -> int | None:
    """Re-add one shelf from the sizes counted on it, then re-add the group.

    A shelf with no variant rows is left alone. That is a shelf nobody has
    broken down, not a shelf holding nothing, and its item-level count is the
    only figure anybody has entered for it.
    """
    rows = (
        await session.scalars(
            select(StockAtVariant).where(
                StockAtVariant.sku == sku, StockAtVariant.location_code == location
            )
        )
    ).all()
    if not rows:
        return None
    total = sum(max(0, r.on_hand) for r in rows)
    shelf = await session.get(StockAtLocation, (sku, location))
    if shelf is None:
        shelf = StockAtLocation(sku=sku, location_code=location)
        session.add(shelf)
        await session.flush()
    if shelf.on_hand != total:
        shelf.on_hand = total
        session.add(
            StockLevel(
                sku=sku, on_hand=total, on_order=shelf.on_order,
                recorded_at=occurred, location_code=location,
            )
        )
    await retotal(session, sku, occurred=occurred)
    return total


async def variants_at(session: AsyncSession, sku: str) -> dict[str, dict[str, int]]:
    """Every size counted on every shelf for one item, as location to size to units.

    Absent rather than zero where nothing has been counted, so a caller can tell
    "this shelf holds none of that size" from "nobody has looked".
    """
    out: dict[str, dict[str, int]] = {}
    for row in await session.scalars(
        select(StockAtVariant).where(StockAtVariant.sku == sku)
    ):
        out.setdefault(row.location_code, {})[row.variant] = row.on_hand
    return out


async def retotal(session: AsyncSession, sku: str, *, occurred: datetime) -> int | None:
    """Re-add the shelves into the group's total, and record the reading.

    The total is what the buying desk reads and what demand is divided by, and it
    has to be the sum of the places the stock actually is — otherwise an item
    counted in two shops and a storefront is bought against a third number that
    matches none of them.

    An item with no shelf rows at all is left alone. That is not an item holding
    nothing, it is an item nobody has split yet, and its total is the only figure
    anybody has entered for it.
    """
    shelves = (
        await session.scalars(select(StockAtLocation).where(StockAtLocation.sku == sku))
    ).all()
    if not shelves:
        return None
    total = sum(max(0, s.on_hand) for s in shelves)
    snapshot = await session.get(StockSnapshot, sku)
    if snapshot is None:
        snapshot = StockSnapshot(sku=sku)
        session.add(snapshot)
        await session.flush()
    if snapshot.on_hand == total:
        return total
    snapshot.on_hand = total
    session.add(
        StockLevel(
            sku=sku, on_hand=total, on_order=snapshot.on_order, recorded_at=occurred
        )
    )
    return total


def lines_from_payload(payload: dict) -> dict[str, int]:
    """SKU to units from an order payload, in either shape the platform emits.

    Lines with no SKU are dropped rather than guessed at. They are already
    counted separately as ``lines_without_sku`` — a storefront selling a product
    nobody gave a SKU to is a real and visible problem, and inventing a match
    from the product title would bury it under a number that looks fine.
    """
    for candidate in (payload, payload.get("order") or {}):
        lines = candidate.get("line_items")
        if isinstance(lines, list):
            break
    else:
        return {}

    quantities: dict[str, int] = {}
    for line in lines:
        sku = (line.get("sku") or "").strip()
        if not sku:
            continue
        try:
            quantity = int(line.get("quantity") or 0)
        except (TypeError, ValueError):
            continue
        if quantity > 0:
            quantities[sku] = quantities.get(sku, 0) + quantity
    return quantities


async def sell_known_only(
    session: AsyncSession,
    quantities: dict[str, int],
    *,
    occurred: datetime,
    location: str = DEFAULT_LOCATION,
) -> list[Movement]:
    """As ``sell``, but silently ignoring SKUs this platform does not stock.

    A webhook is not a form and has nobody to correct: refusing the whole order
    because one line names an item the catalogue has never heard of would lose
    the customer, the demand and the stock movement for every other line on it.
    The unknown SKU still reaches the event payload, where it is visible.
    """
    if not quantities:
        return []
    known = set(
        (
            await session.scalars(select(Item.sku).where(Item.sku.in_(list(quantities))))
        ).all()
    )
    stocked = {k: v for k, v in quantities.items() if k in known}
    return await sell(session, stocked, occurred=occurred, location=location)
