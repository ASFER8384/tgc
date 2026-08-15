"""Where the stock actually is, rather than how much of it there is.

The catalogue endpoint answers "how many", which is the number buying acts on. It
cannot answer the question a shop has — whether the one left is the one in front
of them or in the other city — and it could not see the storefront's count at all,
so the group's total was short by everything Shopify held.

Split out rather than folded into ``/items`` because the two are read by different
people for different reasons, and because this one can fail in a way that one
must not: a storefront that is unreachable has to leave the buying desk working.
"""

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from pydantic import Field as FieldSpec
from sqlalchemy import select

from sca.api.deps import ActorDep, SessionDep, SettingsDep
from sca.models import (
    AuditLog,
    Item,
    ShopifyVariant,
    StockAtLocation,
    StockAtVariant,
    StockLocation,
    StockSnapshot,
)

router = APIRouter(prefix="/inventory", tags=["inventory"])


def _variant(row: ShopifyVariant) -> dict:
    return {
        "variant_id": row.variant_id,
        "sku": row.sku,
        "product": row.product_title,
        "title": row.variant_title,
        "options": row.options or [],
        "price": str(row.price),
        "currency": row.currency,
        "on_hand": row.on_hand,
        "tracked": row.tracked,
        "status": row.status,
        "vendor": row.vendor,
    }


@router.get("")
async def inventory(session: SessionDep, actor: ActorDep, settings: SettingsDep) -> dict:
    """Every item, every shelf it sits on, and what the storefront says it holds."""
    from sca.shopify import ONLINE, configured

    items = list(await session.scalars(select(Item).order_by(Item.sku)))
    snapshots = {s.sku: s for s in await session.scalars(select(StockSnapshot))}
    places = list(await session.scalars(select(StockLocation).order_by(StockLocation.code)))
    by_code = {p.code: p for p in places}

    shelves: dict[str, dict[str, int]] = {}
    for row in await session.scalars(select(StockAtLocation)):
        shelves.setdefault(row.sku, {})[row.location_code] = row.on_hand

    # The size breakdown, where a shelf has one. Absent rather than zero, so the
    # page can tell "this shelf holds none of that size" from "nobody has
    # counted it by size" — which are opposite instructions to a shop.
    counted: dict[str, dict[str, dict[str, int]]] = {}
    for row in await session.scalars(select(StockAtVariant)):
        counted.setdefault(row.sku, {}).setdefault(row.location_code, {})[row.variant] = row.on_hand

    variants: dict[str, list[ShopifyVariant]] = {}
    orphans: list[ShopifyVariant] = []
    known = {i.sku for i in items}
    synced: datetime | None = None
    for row in await session.scalars(
        select(ShopifyVariant).order_by(ShopifyVariant.product_title, ShopifyVariant.variant_title)
    ):
        synced = row.synced_at if synced is None or row.synced_at > synced else synced
        if row.sku and row.sku in known:
            variants.setdefault(row.sku, []).append(row)
        else:
            # Stock on the website that belongs to no item here. Reported rather
            # than dropped: a product with no SKU sells perfectly well and is
            # invisible to every count on this platform, which is exactly the
            # kind of gap that is only ever found by running out.
            orphans.append(row)

    out = []
    for item in items:
        snapshot = snapshots.get(item.sku)
        split = shelves.get(item.sku, {})
        sizes = counted.get(item.sku, {})
        rows = [
            {
                "code": code,
                "name": by_code[code].name if code in by_code else code,
                "kind": by_code[code].kind if code in by_code else "retail",
                "on_hand": on_hand,
                # What has been counted size by size on this shelf, and nothing
                # at all where nobody has. An empty object is a shelf whose
                # total is still the only figure anybody entered for it.
                "variants": sizes.get(code, {}),
            }
            for code, on_hand in sorted(split.items())
        ]
        online = sum(r["on_hand"] for r in rows if r["kind"] == "online")
        in_house = sum(r["on_hand"] for r in rows if r["kind"] != "online")
        mine = [_variant(v) for v in variants.get(item.sku, [])]
        out.append({
            "sku": item.sku,
            "name": item.name,
            "brand": item.brand,
            "category": item.category,
            "unit": item.unit,
            # The group's figure as stored, beside what the shelves add up to.
            # Both, because a disagreement between them is the thing worth
            # seeing and a single reconciled number would hide it.
            "total": snapshot.on_hand if snapshot else 0,
            "shelf_total": online + in_house,
            "on_order": snapshot.on_order if snapshot else 0,
            "online": online,
            "in_house": in_house,
            "locations": rows,
            # An item nobody has split yet is not an item holding nothing. Its
            # total is the only figure anybody entered and the page has to say
            # so rather than draw three empty shelves.
            "split": bool(rows),
            "variants": mine,
        })

    # Every brand name in play, from both sides. Offered on the item form so a
    # fourth brand needs no deployment, and so the spelling that has to match a
    # Shopify vendor exactly is not typed from memory.
    names = {i.brand for i in items if i.brand}
    vendor_by_sku = {
        sku: rows[0].vendor for sku, rows in variants.items() if rows[0].vendor
    }
    names.update(v.lower() for v in vendor_by_sku.values())

    return {
        "items": out,
        "brands": sorted(names),
        "vendor_by_sku": vendor_by_sku,
        "locations": [
            {"code": p.code, "name": p.name, "kind": p.kind, "active": p.active}
            for p in places
        ],
        "shopify": {
            "connected": configured(settings),
            "domain": settings.shopify_shop_domain,
            "online_location": ONLINE,
            "last_synced": synced.isoformat() if synced else None,
            "variants": sum(len(v) for v in variants.values()) + len(orphans),
            # Everything the storefront sells that this platform cannot count.
            "unmatched": [_variant(v) for v in orphans],
        },
    }


class CountIn(BaseModel):
    sku: str
    location: str
    on_hand: int = FieldSpec(ge=0)


@router.post("/count")
async def record_count(body: CountIn, session: SessionDep, actor: ActorDep) -> dict:
    """Somebody has walked the rail and counted. Write what they found.

    A count, not a movement: the figure replaces what was there rather than
    adjusting it, and the group's total is re-added from the shelves afterwards.
    Adjusting by a difference would carry whatever was wrong with the old figure
    forward forever, which is the failure this exists to end.

    Retail shelves only. The storefront's count belongs to Shopify — it moves on
    checkouts, refunds and fulfilments this platform is never told about, so a
    figure accepted here would be overwritten by the next pull. A screen that
    takes a number it intends to discard is worse than one that does not offer
    the box, so this refuses instead of pretending.
    """
    from sca.stock import set_shelf

    item = await session.scalar(select(Item).where(Item.sku == body.sku))
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no item with SKU {body.sku}")
    place = await session.get(StockLocation, body.location)
    if place is None or not place.active:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no active location {body.location}")
    if place.kind == "online":
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"{place.name} is counted by Shopify. Change it there — a figure saved "
            "here would be overwritten the next time the storefront is read.",
        )

    now = datetime.now(UTC)
    was, becomes = await set_shelf(session, body.sku, body.location, body.on_hand, occurred=now)
    snapshot = await session.get(StockSnapshot, body.sku)
    session.add(AuditLog(
        actor=actor,
        action="inventory.count",
        entity="stock",
        entity_id=f"{body.sku}@{body.location}",
        meta={"was": was, "now": becomes},
    ))
    return {
        "sku": body.sku,
        "location": body.location,
        "was": was,
        "now": becomes,
        "total": snapshot.on_hand if snapshot else becomes,
    }


class VariantCountIn(BaseModel):
    sku: str
    location: str
    variant: str
    on_hand: int = FieldSpec(ge=0)


@router.post("/count/variant")
async def record_variant_count(
    body: VariantCountIn, session: SessionDep, actor: ActorDep
) -> dict:
    """Count one size on one shelf.

    Same rule as the shelf count above, one level down: what is written is what
    somebody found, and everything above it is re-added rather than adjusted.

    Worth knowing before the first one is saved: once any size is counted on a
    shelf, that shelf's total becomes the sum of its sizes. A rail of three where
    one Small is counted and nobody finishes now reads as one. Deliberate — the
    alternative is carrying two units nobody counted inside a total everybody
    trusts, which nothing would ever flag as wrong.
    """
    from sca.stock import set_variant

    item = await session.scalar(select(Item).where(Item.sku == body.sku))
    if item is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no item with SKU {body.sku}")
    place = await session.get(StockLocation, body.location)
    if place is None or not place.active:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no active location {body.location}")
    if place.kind == "online":
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"{place.name} is counted by Shopify, size by size. Change it there — "
            "a figure saved here would be overwritten the next time it is read.",
        )
    if not body.variant.strip():
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Name the size or shade being counted. A count against a blank one "
            "would be a fourth kind of total nobody could reconcile.",
        )

    now = datetime.now(UTC)
    was, becomes = await set_variant(
        session, body.sku, body.location, body.variant, body.on_hand, occurred=now
    )
    shelf = await session.get(StockAtLocation, (body.sku, body.location))
    snapshot = await session.get(StockSnapshot, body.sku)
    session.add(AuditLog(
        actor=actor,
        action="inventory.count.variant",
        entity="stock",
        entity_id=f"{body.sku}/{body.variant}@{body.location}",
        meta={"was": was, "now": becomes},
    ))
    return {
        "sku": body.sku,
        "location": body.location,
        "variant": body.variant.strip(),
        "was": was,
        "now": becomes,
        # Both levels above, so a caller can see what its one number did.
        "shelf": shelf.on_hand if shelf else becomes,
        "total": snapshot.on_hand if snapshot else becomes,
    }


@router.post("/shopify/pull")
async def pull_from_shopify(
    session: SessionDep, actor: ActorDep, settings: SettingsDep, apply: bool = False
) -> dict:
    """Read the storefront. With ``apply``, move the online shelf to match it.

    Defaults to reporting only. This changes the number every reorder is decided
    against, and the first time anybody points it at a live store they should be
    able to see what it intends to do before it does it.
    """
    from sca.shopify import pull

    now = datetime.now(UTC)
    report = await pull(session, settings, apply=apply, now=now)
    if apply and report.ok:
        session.add(AuditLog(
            actor=actor,
            action="inventory.shopify.pull",
            entity="stock",
            entity_id="online",
            meta={
                "shop": report.shop,
                "variants": report.variants,
                "changed": [c.sku for c in report.changes],
            },
        ))
    return report.as_dict()
