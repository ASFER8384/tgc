"""Sales rung up in the shop.

Everything the platform knows about demand arrives from Shopify, which means it
knows about the half of the business that happens online. A shop sale currently
leaves two traces, both of them absent: the item does not appear in demand, and
the stock that walked out of the door is only noticed the next time somebody
counts the shelf by hand.

That second absence is the expensive one. Weekly demand is measured over the
weeks an item was *sellable*, read from the stock ledger, and an item that sold
out on Tuesday with nobody recording it looks like an item nobody wanted for the
rest of the week. The correction that exists in ``planning.demand`` can only work
if something writes the ledger, and in a shop the only moment anybody knows the
shelf changed is the moment of the sale.

So one action writes both: the canonical ``order_paid`` event the customer half
already understands, and the new stock position the supplier half plans against.
Either both land or neither does — they share the request transaction, and a sale
recorded without its stock movement would quietly poison the very correction it
was captured for.
"""

from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from cdp.config import get_settings as get_cdp_settings
from cdp.ingest.schemas import CanonicalEvent
from cdp.models import Event, Person
from sca.api.deps import ActorDep, SessionDep
from sca.models import AuditLog, Item
from sca.stock import DEFAULT_LOCATION, sell

router = APIRouter(tags=["sales"])

# Which till. A shop with two counters wants two standing records rather than
# one, so a stocktake dispute can be narrowed to the till that rang it.
DEFAULT_TILL = "counter"

# Sources whose shelf is the storefront by definition. An order placed on the
# website came off the website's stock, and there is nothing for a caller to say.
ONLINE_SOURCES = {"shopify"}

# Sources with no shelf of their own. The mall stand is a demonstration entry
# point rather than a stockroom — nothing is held there, and giving it a shelf
# would invent a stock position the group does not have.
UNSHELVED_SOURCES = {"activation"}


def _shelf_for(source: str, location: str | None) -> str:
    """Which shelf this sale came off.

    Derived from the source rather than defaulted to one shelf for everything.
    A blanket default of the storefront was wrong twice over for a counter sale:
    the shop that actually sold the units kept its number, and the storefront
    decrement was thrown away at the next pull, since Shopify overwrites that
    shelf with its own count. The stock left the building and no record moved.

    So a shop has to name itself. It is the one caller that knows, the till now
    always sends it, and a sale that cannot say where it happened is better
    refused than filed against a shelf picked by a default.
    """
    if location:
        return location
    if source in ONLINE_SOURCES or source in UNSHELVED_SOURCES:
        return DEFAULT_LOCATION
    raise HTTPException(
        status.HTTP_422_UNPROCESSABLE_ENTITY,
        f"Say which shop this sale came off — a {source} sale has a shelf, and "
        "guessing one takes the stock off somewhere it never was.",
    )


class SaleLine(BaseModel):
    sku: str
    quantity: int = Field(gt=0)
    # Which size, which shade. Carried but never used to identify the item: a
    # variant is a fact about the basket, and treating it as part of the key
    # would split one line's demand across five rows and put every size below
    # the threshold that triggers an order. Shopify sends this on its own line
    # items and the connector keeps the whole payload; without it here the two
    # channels would disagree about how much they know.
    variant: str | None = None
    # What it actually sold for, per unit. Optional because a shop assistant
    # recording yesterday's basket may not have the figure to hand, and a sale
    # with no price is still demand. Never defaulted to the item's unit cost:
    # that is what it cost us, and writing it here would file our own cost as
    # the customer's payment and overstate margin on every line.
    unit_price: Decimal | None = None


class SaleIn(BaseModel):
    lines: list[SaleLine]
    # Whoever bought it, if she said. One is enough; both are better.
    phone: str | None = None
    email: str | None = None
    name: str | None = None
    till: str = DEFAULT_TILL
    currency: str = "SAR"
    # Which counter the sale came over. Carried because a sale is a sale wherever
    # it was rung up, and this endpoint is the one place that knows how to write
    # one — the customer console records the same thing from the other side of the
    # business, and it should not have to reimplement the stock movement or the
    # line-item shape to do it.
    source: str = "pos"
    channel: str = "retail"
    # Whether the stock ledger moves. On by default, because a sale that left the
    # shop took something with it. Off for a sale recorded after the fact against
    # a shelf that was already counted down, where taking it off twice would
    # invent a stockout nobody had.
    move_stock: bool = True
    # Which shelf this came off. A sale in Jeddah cannot take stock out of
    # Riyadh, and the total is only truthful if each sale names its own shelf.
    #
    # No default. It used to be the storefront for everything, which quietly
    # took counter sales off a shelf they never touched. Where the source
    # already answers the question — a website order, the mall stand — it is
    # filled in from that; a shop has to say.
    location: str | None = None
    # The form's own idea of which sale this is, so that pressing Save twice —
    # or pressing it once on a slow connection — records one basket rather than
    # two. Generated by the browser when the form opens and unchanged until it
    # closes, which is the only place that knows the difference between a retry
    # and a second customer buying the same thing.
    receipt: str | None = None
    # When it was sold, where that is not now. A morning's paper receipts typed
    # up at lunchtime are history, and stamping them all with lunchtime would put
    # a day's demand in one minute and one week's stock movement in the wrong
    # order.
    occurred_at: datetime | None = None


class SaleOut(BaseModel):
    accepted: bool
    duplicate: bool
    receipt: str
    person_id: str | None
    identified: bool
    units: int
    value: str
    currency: str
    # Per SKU, the new position and anything that did not add up. Returned rather
    # than logged only, because the person holding the item is the only one who
    # can reconcile a shelf that disagrees with the record.
    stock: list[dict]
    notes: list[str]


def _counter_name(till: str) -> str:
    return f"Walk-in — {till}"


@router.post("/sales", response_model=SaleOut, status_code=status.HTTP_201_CREATED)
async def record_sale(body: SaleIn, session: SessionDep, actor: ActorDep) -> SaleOut:
    """One basket: what left the shop, and who took it if she said.

    Imported here rather than at module scope so the supplier half does not pull
    the whole identity and profile stack in at import time; this is the only
    route in it that needs them.
    """
    from cdp.ingest.service import IngestService

    if not body.lines:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "a sale needs at least one line")

    # One line per SKU. Two lines for the same item is a legitimate way to ring
    # up a basket, but it makes the stock arithmetic below read twice from a
    # position it has already changed, so they are added together first.
    merged: dict[str, SaleLine] = {}
    for line in body.lines:
        sku = line.sku.strip()
        if not sku:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "a line needs a SKU")
        existing = merged.get(sku)
        if existing is None:
            merged[sku] = SaleLine(
                sku=sku, quantity=line.quantity, unit_price=line.unit_price,
                variant=line.variant,
            )
            continue
        existing.quantity += line.quantity
        if existing.variant and line.variant and existing.variant != line.variant:
            # Two sizes of one item in one basket. The demand is the item's, and
            # this form has one variant slot — say so rather than file both under
            # whichever was typed first.
            existing.variant = "mixed"
        elif existing.variant is None:
            existing.variant = line.variant
        # The first price given holds. Two different prices for one SKU in one
        # basket is a discount on part of it, which this form cannot express and
        # must not silently average.
        if existing.unit_price is None:
            existing.unit_price = line.unit_price

    items = {
        item.sku: item
        for item in await session.scalars(select(Item).where(Item.sku.in_(list(merged))))
    }
    unknown = sorted(set(merged) - set(items))
    if unknown:
        # Refused rather than recorded against a SKU nobody sells. A typo here
        # would appear in demand as a product that does not exist, and would be
        # bought from a supplier who has never heard of it.
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"no such item: {', '.join(unknown)}"
        )

    occurred = body.occurred_at or datetime.now(UTC)
    if occurred.tzinfo is None:
        occurred = occurred.replace(tzinfo=UTC)
    receipt = (body.receipt or "").strip() or f"{occurred.isoformat(timespec='seconds')}"
    # Resolved before anything is written. A sale that cannot name its shelf is
    # refused whole rather than recorded against the customer and then found to
    # have nowhere to take the stock from.
    location = _shelf_for(body.source, body.location)

    identified = bool((body.phone or "").strip() or (body.email or "").strip())
    if identified:
        identifiers: dict[str, str | None] = {"phone": body.phone, "email": body.email}
    else:
        # The till itself, and never alongside a real identifier. Sent together,
        # the counter would be weak evidence linking one customer to the standing
        # record, the record would inherit her phone number, and from then on
        # every anonymous sale in the shop would resolve to her.
        identifiers = {"pos_counter": f"counter:{body.till}"}


    lines_payload = []
    value = Decimal("0")
    units = 0
    unpriced: list[str] = []
    brands: dict[str, Decimal] = {}
    for sku, line in merged.items():
        item = items[sku]
        units += line.quantity
        if line.unit_price is None:
            unpriced.append(sku)
        else:
            total = line.unit_price * line.quantity
            value += total
            brand = (item.brand or "unassigned").lower()
            brands[brand] = brands.get(brand, Decimal("0")) + total
        lines_payload.append({
            # The three field names ``planning.demand`` reads. Matching the
            # vocabulary the Shopify connector already emits is what lets one
            # demand query serve both channels without branching on source.
            "sku": sku,
            "quantity": line.quantity,
            "price": str(line.unit_price) if line.unit_price is not None else None,
            "name": item.name,
            "brand": item.brand,
            # Shopify's own field name, so one reader serves both channels.
            "variant_title": line.variant,
        })

    event = CanonicalEvent(
        source=body.source,
        name="order_paid",
        dedupe_key=f"{body.source}:{body.till}:{receipt}",
        occurred_at=occurred,
        identifiers=identifiers,
        value_amount=value,
        currency=body.currency,
        channel=body.channel,
        brands=brands,
        display_name=body.name if identified else None,
        payload={
            "line_items": lines_payload,
            "order_id": receipt,
            "till": body.till,
            "recorded_by": actor,
            # Which shop rang it up, in the record rather than only in the stock
            # movement. The shelf already knew — it had to, to take the units off
            # the right one — but the customer's history did not, so a timeline
            # could say she bought an abaya and not that she bought it in Jeddah.
            #
            # Written without the ``location_backfilled`` flag the seeded history
            # carries, and the difference is the point: this one was recorded at
            # the till by the person who served her. The old rows were assigned
            # afterwards by a script, and nothing should ever have to guess later
            # which of those it is reading.
            "location": location,
            # Said in the record itself, not inferred later from an absent phone
            # number, so a sale that simply lost its identifier in a merge cannot
            # be mistaken for one that never had one.
            "anonymous": not identified,
        },
    )

    service = IngestService(
        session, actor=actor, country_code=get_cdp_settings().default_country_code
    )
    result = await service.ingest(event, moves_stock=False)

    if result.duplicate:
        # The same receipt again. The stock was already taken off on the first
        # one, and taking it off twice is how a double tap on a slow connection
        # empties a shelf that is still full.
        return SaleOut(
            accepted=True, duplicate=True, receipt=receipt, person_id=result.person_id,
            identified=identified, units=units, value=str(value), currency=body.currency,
            stock=[], notes=["Already recorded — this receipt was saved before. Nothing changed."],
        )

    if not identified and result.person_id:
        person = await session.get(Person, result.person_id)
        if person is not None and not person.synthetic:
            person.synthetic = True
            person.display_name = _counter_name(body.till)

    if body.move_stock:
        movements = await sell(
            session, {k: v.quantity for k, v in merged.items()},
            occurred=occurred, location=location,
        )
        stock_out = [m.as_dict() for m in movements]
        notes = [m.shortfall for m in movements if m.shortfall]
    else:
        stock_out, notes = [], [
            "Stock was not changed — this sale was recorded against a shelf that "
            "had already been counted down."
        ]
    if unpriced:
        notes.append(
            f"No price on {', '.join(sorted(unpriced))} — counted as demand, "
            "not as revenue."
        )

    session.add(
        AuditLog(
            actor=actor, action="sale.recorded", entity="sale", entity_id=receipt,
            meta={
                "till": body.till,
                "identified": identified,
                "units": units,
                "value": str(value),
                "lines": [{"sku": s, "quantity": line.quantity} for s, line in merged.items()],
                # Kept in the log rather than only in the reply: a shelf that
                # disagreed with the record is the sort of thing somebody wants
                # to look back at a fortnight later, when the count is repeated.
                "short": [n for n in notes if "more than" in n],
            },
        )
    )
    await session.flush()

    return SaleOut(
        accepted=True, duplicate=False, receipt=receipt, person_id=result.person_id,
        identified=identified, units=units, value=str(value), currency=body.currency,
        stock=stock_out, notes=notes,
    )


@router.get("/sales")
async def recent_sales(session: SessionDep, actor: ActorDep, limit: int = 25) -> list[dict]:
    """What has been rung up, most recent first.

    Only what was typed here — the shop's own record, so somebody can see that
    the last basket landed and catch a mistyped quantity while the customer is
    still in front of them.
    """
    rows = await session.scalars(
        select(Event)
        .where(Event.source == "pos", Event.name == "order_paid")
        .order_by(Event.occurred_at.desc())
        .limit(max(1, min(limit, 200)))
    )
    out = []
    for event in rows:
        payload = event.payload or {}
        lines = payload.get("line_items") or []
        out.append({
            "receipt": payload.get("order_id"),
            "occurred_at": event.occurred_at.isoformat(),
            "till": payload.get("till"),
            "anonymous": bool(payload.get("anonymous")),
            "units": sum(int(line.get("quantity") or 0) for line in lines),
            "value": str(event.value_amount or "0"),
            "currency": event.currency,
            "lines": [
                {"sku": line.get("sku"), "quantity": line.get("quantity"),
                 "name": line.get("name")}
                for line in lines
            ],
        })
    return out
