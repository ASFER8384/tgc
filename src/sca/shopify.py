"""Read the storefront's own count, and let it own the online shelf.

The platform holds four SKUs and could not say how many of each the group had,
because it only knew about the ones in the shops. Shopify held the rest and was
never asked, so the abaya read as 1 while 500 sat against the same SKU on the
website. Both numbers were right about their own shelf and neither was the
group's — and the smaller of them was the one on the buying desk, which is the
direction that hurts: it reorders stock that is already in the building.

**One direction only.** Shopify moves its own count on a checkout, a refund, a
fulfilment and a manual correction in its admin, and this platform is told about
at most the first of those. A figure kept here that tried to lead theirs would be
wrong within a day and would then let the website sell what it does not have. So
the storefront is the record for the storefront's shelf, the shops are this
platform's, and the group's total is what those add up to.

That leaves one thing this cannot do, said plainly rather than worked around:
correcting the online count has to happen in Shopify. A count typed here would be
overwritten by the next pull, and a screen that accepts a number it intends to
discard is worse than one that does not offer the box.

**Variants are kept whole.** A per-SKU sum is what the arithmetic needs and not
what a person is asking. Twelve abayas is one Small and eleven Large, and only
the first of those explains why the size a customer wanted was missing.
"""

from dataclasses import dataclass, field
from datetime import datetime

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sca.config import Settings
from sca.models import Item, ShopifyVariant
from sca.stock import retotal, set_shelf

# The shelf a storefront's stock lands on. Shopify presents itself as one place
# to sell from even where it has several warehouses behind it, and the promise it
# makes to a customer is made against the whole of it.
ONLINE = "online"

# One page of products, and the variants under each. Both are asked with their
# page info so a truncated read is reported rather than silently short — a
# missing page of variants is a stock figure that is too low, and too low is the
# one that quietly reorders.
_QUERY = """
query Catalogue($cursor: String) {
  products(first: 50, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id
      title
      handle
      vendor
      status
      variants(first: 100) {
        pageInfo { hasNextPage }
        nodes {
          id
          title
          sku
          price
          inventoryQuantity
          inventoryItem { tracked }
          selectedOptions { name value }
        }
      }
    }
  }
}
"""

_SHOP = "query { shop { name currencyCode } }"


@dataclass
class RemoteVariant:
    variant_id: str
    product_id: str
    sku: str | None
    product_title: str
    handle: str | None
    vendor: str | None
    status: str | None
    variant_title: str | None
    options: list[dict]
    price: float
    on_hand: int
    tracked: bool


@dataclass
class Change:
    sku: str
    was: int
    now: int


@dataclass
class PullReport:
    """What a pull found, and what it would do — the same shape either way.

    A dry run and a real one differ in whether anything was written, not in what
    they say. Somebody deciding whether to let this touch their stock has to be
    able to read the consequences first.
    """

    ok: bool = True
    shop: str | None = None
    currency: str | None = None
    applied: bool = False
    error: str | None = None

    products: int = 0
    variants: int = 0
    changes: list[Change] = field(default_factory=list)
    # Variants Shopify has with no SKU on them. Their units belong to no item
    # here and are in no total, which is a fault worth naming — a product added
    # in Shopify's admin without a SKU sells fine and is invisible to buying.
    without_sku: list[str] = field(default_factory=list)
    # SKUs the storefront sells and this catalogue has never heard of, and the
    # reverse. Neither is an error; both are things somebody should look at.
    unknown_skus: list[str] = field(default_factory=list)
    not_on_shopify: list[str] = field(default_factory=list)
    # Where the catalogue's brand and the storefront's vendor disagree. Reported,
    # never silently corrected: the vendor drives which brand a customer's spend
    # is filed under, and rewriting a brand label from a webhook payload would
    # move somebody's purchase history between brands without anybody deciding to.
    brand_mismatch: list[dict] = field(default_factory=list)
    # Products whose variant list came back truncated. Named individually,
    # because the fix is per product and a count would not say which.
    truncated: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "shop": self.shop,
            "currency": self.currency,
            "applied": self.applied,
            "error": self.error,
            "products": self.products,
            "variants": self.variants,
            "changes": [
                {"sku": c.sku, "was": c.was, "now": c.now} for c in self.changes
            ],
            "without_sku": self.without_sku,
            "unknown_skus": self.unknown_skus,
            "not_on_shopify": self.not_on_shopify,
            "brand_mismatch": self.brand_mismatch,
            "truncated": self.truncated,
        }


def configured(settings: Settings) -> bool:
    return bool(settings.shopify_shop_domain and settings.shopify_admin_token)


def _endpoint(settings: Settings) -> str:
    domain = (settings.shopify_shop_domain or "").strip()
    # Both spellings accepted. Shopify shows the admin one with the scheme on it
    # and people paste what they see; refusing that would be a support ticket
    # rather than a safety measure.
    domain = domain.removeprefix("https://").removeprefix("http://").rstrip("/")
    return f"https://{domain}/admin/api/{settings.shopify_api_version}/graphql.json"


async def _call(client: httpx.AsyncClient, url: str, token: str, query: str,
                variables: dict | None = None) -> dict:
    response = await client.post(
        url,
        json={"query": query, "variables": variables or {}},
        headers={"X-Shopify-Access-Token": token, "Content-Type": "application/json"},
        timeout=30.0,
    )
    response.raise_for_status()
    body = response.json()
    # GraphQL answers 200 with an errors array, so the status code alone is not
    # a success test — a bad token comes back as a perfectly healthy 200.
    if body.get("errors"):
        first = body["errors"][0]
        raise RuntimeError(str(first.get("message") or first))
    return body["data"]


async def fetch(settings: Settings) -> tuple[list[RemoteVariant], dict]:
    """Every variant on the store, with the shop's name and currency."""
    url, token = _endpoint(settings), settings.shopify_admin_token or ""
    out: list[RemoteVariant] = []
    truncated: list[str] = []
    async with httpx.AsyncClient() as client:
        shop = (await _call(client, url, token, _SHOP))["shop"]
        cursor, products = None, 0
        while True:
            page = (await _call(client, url, token, _QUERY, {"cursor": cursor}))["products"]
            for node in page["nodes"]:
                products += 1
                variants = node["variants"]
                if variants["pageInfo"]["hasNextPage"]:
                    truncated.append(node["title"])
                for variant in variants["nodes"]:
                    sku = (variant.get("sku") or "").strip() or None
                    item = variant.get("inventoryItem") or {}
                    out.append(RemoteVariant(
                        variant_id=variant["id"],
                        product_id=node["id"],
                        sku=sku,
                        product_title=node.get("title") or "",
                        handle=node.get("handle"),
                        vendor=node.get("vendor"),
                        status=node.get("status"),
                        variant_title=variant.get("title"),
                        options=variant.get("selectedOptions") or [],
                        price=float(variant.get("price") or 0),
                        # Shopify reports null where it does not track, and a
                        # null read as zero is an empty shelf that is not empty.
                        on_hand=int(variant.get("inventoryQuantity") or 0),
                        tracked=bool(item.get("tracked", True)),
                    ))
            if not page["pageInfo"]["hasNextPage"]:
                break
            cursor = page["pageInfo"]["endCursor"]
    return out, {"shop": shop, "products": products, "truncated": truncated}


async def pull(
    session: AsyncSession, settings: Settings, *, apply: bool, now: datetime
) -> PullReport:
    """Read the storefront and, if asked, move the online shelf to match."""
    if not configured(settings):
        return PullReport(
            ok=False,
            error="No storefront connected. Set SCA_SHOPIFY_SHOP_DOMAIN and "
                  "SCA_SHOPIFY_ADMIN_TOKEN (a custom app token with read_products "
                  "and read_inventory).",
        )
    try:
        remote, meta = await fetch(settings)
    except httpx.HTTPStatusError as exc:
        # 401 is the one that will actually happen, and "unauthorised" alone
        # sends somebody to check the wrong thing.
        detail = {
            401: "Shopify rejected the token. Check it is an Admin API token "
                 "(shpat_…) for this shop and has not been revoked.",
            403: "The token is valid but lacks a scope. It needs read_products "
                 "and read_inventory.",
            404: "No store at that domain, or the API version is retired.",
        }.get(exc.response.status_code)
        return PullReport(ok=False, error=detail or f"Shopify said {exc.response.status_code}.")
    except (httpx.HTTPError, RuntimeError) as exc:
        return PullReport(ok=False, error=str(exc))

    report = PullReport(
        shop=meta["shop"].get("name"),
        currency=meta["shop"].get("currencyCode"),
        products=meta["products"],
        variants=len(remote),
        truncated=meta["truncated"],
        applied=apply,
    )
    report.without_sku = sorted({
        f"{v.product_title} — {v.variant_title}".strip(" —")
        for v in remote if not v.sku
    })

    catalogue = {i.sku: i for i in await session.scalars(select(Item))}
    known = set(catalogue)

    # The vendor is what decides which brand a sale is filed under, so a vendor
    # that disagrees with the catalogue is a customer's spend landing under the
    # wrong brand — and it is invisible from either side on its own.
    seen_vendor: dict[str, str] = {}
    for variant in remote:
        if variant.sku and variant.vendor:
            seen_vendor.setdefault(variant.sku, variant.vendor)
    for sku, vendor in sorted(seen_vendor.items()):
        item = catalogue.get(sku)
        if item is None:
            continue
        if (item.brand or "").strip().lower() != vendor.strip().lower():
            report.brand_mismatch.append(
                {"sku": sku, "here": item.brand, "shopify": vendor}
            )
    # Untracked variants contribute nothing rather than zero. Shopify reports no
    # quantity for them and they sell without one, so counting their zero would
    # write a real shelf down to nothing.
    counted: dict[str, int] = {}
    for variant in remote:
        if variant.sku and variant.tracked:
            counted[variant.sku] = counted.get(variant.sku, 0) + max(0, variant.on_hand)

    report.unknown_skus = sorted({v.sku for v in remote if v.sku} - known)
    report.not_on_shopify = sorted(known - {v.sku for v in remote if v.sku})

    # What the online shelf holds now, so a dry run can say what would move.
    from sca.models import StockAtLocation
    current = {
        row.sku: row.on_hand
        for row in await session.scalars(
            select(StockAtLocation).where(StockAtLocation.location_code == ONLINE)
        )
    }
    for sku in sorted(counted):
        if sku not in known:
            continue
        was, becomes = current.get(sku, 0), counted[sku]
        if was != becomes:
            report.changes.append(Change(sku=sku, was=was, now=becomes))
    # A SKU this platform stocks that the storefront no longer lists holds
    # nothing online. Said as a change so it is visible before it is applied.
    for sku in sorted(known - set(counted)):
        if current.get(sku, 0):
            report.changes.append(Change(sku=sku, was=current[sku], now=0))

    if not apply:
        return report

    # The mirror is replaced, not merged: a variant deleted in Shopify has to
    # disappear here too, or its units stay in a total nobody can find them in.
    seen = {v.variant_id for v in remote}
    for row in await session.scalars(select(ShopifyVariant)):
        if row.variant_id not in seen:
            await session.delete(row)
    for variant in remote:
        row = await session.get(ShopifyVariant, variant.variant_id)
        if row is None:
            row = ShopifyVariant(variant_id=variant.variant_id)
            session.add(row)
        row.product_id = variant.product_id
        row.sku = variant.sku
        row.product_title = variant.product_title
        row.handle = variant.handle
        row.vendor = variant.vendor
        row.status = variant.status
        row.variant_title = variant.variant_title
        row.options = variant.options
        row.price = variant.price
        row.currency = report.currency
        row.on_hand = variant.on_hand
        row.tracked = variant.tracked
        row.synced_at = now

    for change in report.changes:
        await set_shelf(session, change.sku, ONLINE, change.now, occurred=now)
    # Items whose online figure did not move can still need their total re-added:
    # the first pull after locations arrived is one, and so is any item whose
    # shops were counted while the storefront stood still.
    for sku in sorted(known):
        await retotal(session, sku, occurred=now)
    await session.flush()
    return report
