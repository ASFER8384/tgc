"""Shopify import files, for the half of the business that has a storefront.

The seed writes counter and mall trade straight into this platform, because the
counter and the mall have no system of their own. Aleena's storefront does, and
inventing orders on it would mix made-up baskets into the one channel carrying
real ones. So the storefront's side is written out here instead, for somebody to
import into Shopify, after which its webhooks deliver the orders the same way
they always have.

Two files, and the first matters more than the second.

**products.csv** — the four lines with SKUs and variants on them. Every variant
carries the *item's* SKU rather than a variant-level one: this platform keys an
item by its SKU and orders demand against it, and a per-variant SKU would split
one line's demand across five rows so that every size sat below the threshold
that triggers an order. Shopify allows the repeat and warns about it; the size
travels on ``variant_title``, which the connector already keeps.

This file is the fix for the real problem on the live store. Three of the four
orders it has sent so far carry no SKU at all, and a sale with no SKU reaches the
customer's profile and contributes nothing whatsoever to demand.

**customers.csv** — the people, with the brands they buy in the tags.

    .venv/Scripts/python -m scripts.export_shopify_csv --out ./shopify

A warning that is not boilerplate: the customers are invented, and their email
addresses are invented at *real* domains. ``noura.alqahtani0@gmail.com`` is a
plausible address and may belong to somebody. Every row is therefore written
with marketing acceptance set to ``no``, and ``--safe-domains`` rewrites them all
to ``@example.com``, which is reserved by RFC 2606 and cannot deliver anywhere.
Use a development store if the point is only to see the pipeline work.
"""

from __future__ import annotations

import argparse
import csv
import pathlib
import random
from datetime import UTC, date, datetime
from decimal import Decimal

import scripts.seed_sales_history as seed
from scripts.seed_sales_history import (
    CATALOGUE,
    SKU_BRAND,
    VARIANTS,
    Seeder,
    _monday,
    build_customers,
)

# Shopify's own column order for a product import. One row per variant; the
# product-level columns are filled on the first row of each handle and left
# blank afterwards, which is how Shopify reads a multi-variant product.
PRODUCT_COLUMNS = [
    "Handle", "Title", "Body (HTML)", "Vendor", "Type", "Tags", "Published",
    "Option1 Name", "Option1 Value",
    "Variant SKU", "Variant Grams", "Variant Inventory Tracker",
    "Variant Inventory Qty", "Variant Inventory Policy",
    "Variant Fulfillment Service", "Variant Price", "Variant Requires Shipping",
    "Variant Taxable", "Status",
]

# Shopify's own order-export column order, which is what its importers read
# back. One row per line item; the order-level columns are filled on the first
# row and left blank after it.
ORDER_COLUMNS = [
    "Name", "Email", "Financial Status", "Paid at", "Fulfillment Status",
    "Currency", "Subtotal", "Shipping", "Taxes", "Total",
    "Discount Code", "Discount Amount", "Created at",
    "Lineitem quantity", "Lineitem name", "Lineitem price",
    "Lineitem compare at price", "Lineitem sku", "Lineitem variant title",
    "Lineitem requires shipping", "Lineitem taxable",
    "Billing Name", "Billing City", "Billing Country", "Billing Phone",
    "Vendor",
]

CUSTOMER_COLUMNS = [
    "First Name", "Last Name", "Email", "Accepts Email Marketing",
    "Phone", "Accepts SMS Marketing", "City", "Country Code", "Note", "Tags",
]

# What each line is called on a storefront, as against in a stockroom. "Navy
# silk, 140cm" is a fabric spec; nobody buys it under that name.
TITLES = {
    "ALN-ABAYA-01": ("Embroidered Abaya", "Size", "Hand-embroidered abaya in a "
                     "classic cut."),
    "ALN-SILK-NVY": ("Navy Silk Abaya", "Size", "Navy silk, 140cm width, cut and "
                     "finished in Riyadh."),
    "RWS-LIP-TUBE": ("Matte Lipstick", "Shade", "Long-wear matte lipstick in a "
                     "black tube."),
    "AYN-BOX-LUX": ("Luxury Gift Box", "Size", "Rigid outer box with a ribbon "
                    "closure."),
}


def _handle(sku: str) -> str:
    return TITLES[sku][0].lower().replace(" ", "-")


def write_products(path: pathlib.Path) -> int:
    rows = 0
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=PRODUCT_COLUMNS)
        writer.writeheader()
        for sku, brand, _weight, price, _basket, *_ in CATALOGUE:
            title, option, body = TITLES[sku]
            for index, (variant, _share) in enumerate(VARIANTS[sku]):
                row = dict.fromkeys(PRODUCT_COLUMNS, "")
                row["Handle"] = _handle(sku)
                row["Option1 Value"] = variant
                # The item's SKU on every variant. Deliberate — see the module
                # docstring. Shopify warns about the repeat and accepts it.
                row["Variant SKU"] = sku
                row["Variant Grams"] = "0"
                row["Variant Inventory Tracker"] = "shopify"
                row["Variant Inventory Qty"] = "0"
                row["Variant Inventory Policy"] = "deny"
                row["Variant Fulfillment Service"] = "manual"
                row["Variant Price"] = price
                row["Variant Requires Shipping"] = "TRUE"
                row["Variant Taxable"] = "TRUE"
                if index == 0:
                    row["Title"] = title
                    row["Body (HTML)"] = f"<p>{body}</p>"
                    # The vendor string is what the connector maps to a brand.
                    # Written as the brand name so it maps without any
                    # environment override at all.
                    row["Vendor"] = brand
                    row["Type"] = "Apparel" if sku.startswith("ALN") else "Beauty"
                    row["Tags"] = f"{brand.lower()}, tgc"
                    row["Published"] = "TRUE"
                    row["Option1 Name"] = option
                    row["Status"] = "active"
                writer.writerow(row)
                rows += 1
    return rows


def write_customers(path: pathlib.Path, *, count: int, safe: bool, brand: str | None) -> int:
    rng = random.Random(20260811)
    start = _monday(date(2024, 8, 5))
    end = _monday(datetime.now(UTC).date())
    people = build_customers(rng, count, start, end)
    written = 0
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CUSTOMER_COLUMNS)
        writer.writeheader()
        for person in people:
            brands = sorted({SKU_BRAND[sku] for sku in person.affinity})
            if brand and brand not in brands:
                continue
            first, _, last = person.name.partition(" ")
            email = person.email
            if safe:
                email = email.split("@")[0] + "@example.com"
            writer.writerow({
                "First Name": first,
                "Last Name": last,
                "Email": email,
                # Never yes. These addresses are invented at live domains, and a
                # marketing flag on an invented address is a message to whoever
                # actually holds it. Consent for these people is recorded in the
                # platform against the brand that took it; it is not Shopify's
                # to assume.
                "Accepts Email Marketing": "no",
                "Phone": person.phone,
                "Accepts SMS Marketing": "no",
                "City": "Riyadh",
                "Country Code": "SA",
                "Note": f"cadence ~{person.cadence:.0f} weeks; joined {person.joined}",
                "Tags": ", ".join(b.lower() for b in brands) + ", seeded",
            })
            written += 1
    return written


def write_orders(path: pathlib.Path, *, count: int, safe: bool) -> tuple[int, int]:
    """Aleena's storefront trade, which the seeder deliberately does not push.

    Generated by restoring the online share the seeder zeroes out, then keeping
    only the orders that fall to it. These are additional sales rather than a
    slice taken out of the counter's — a brand with a storefront sells more than
    one without, and the platform's store and mall history stands unchanged.

    The order ids sit in their own range so they can collide with nothing: the
    seeder's are 100000s, the live store's are in the trillions.
    """
    original = dict(seed.CHANNEL_MIX)
    try:
        # Only Aleena has a storefront, so only Aleena's lines can go online.
        seed.CHANNEL_MIX = {
            sku: ((0.45, 0.45, 0.10) if SKU_BRAND[sku] == "Aleena" else (0.0, 1.0, 0.0))
            for sku in original
        }
        seeder = Seeder(
            "", "", "", count,
            _monday(date(2024, 8, 5)), _monday(datetime.now(UTC).date()),
            1, False,
        )
        plan, _ = seeder._plan()
    finally:
        seed.CHANNEL_MIX = original

    number = 200_000
    orders = 0
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=ORDER_COLUMNS)
        writer.writeheader()
        for person, placed in sorted(plan.items(), key=lambda kv: kv[0].shopify_id):
            for order in placed:
                if order["where"] != "online":
                    continue
                number += 1
                orders += 1
                email = person.email
                if safe:
                    email = email.split("@")[0] + "@example.com"
                title = TITLES[order["sku"]][0]
                gross = Decimal(order["price"]) * order["quantity"]
                listed = Decimal(order["list_price"]) * order["quantity"]
                stamp = order["when"].strftime("%Y-%m-%d %H:%M:%S %z")
                writer.writerow({
                    "Name": f"#TGC{number}",
                    "Email": email,
                    "Financial Status": "paid",
                    "Paid at": stamp,
                    "Fulfillment Status": "fulfilled",
                    "Currency": "SAR",
                    "Subtotal": str(gross),
                    "Shipping": "0.00",
                    "Taxes": "0.00",
                    "Total": str(gross),
                    "Discount Code": (
                        f"SALE{int(order['discount'] * 100)}" if order["discount"] else ""
                    ),
                    "Discount Amount": str(listed - gross),
                    "Created at": stamp,
                    "Lineitem quantity": order["quantity"],
                    "Lineitem name": f"{title} - {order['variant']}",
                    "Lineitem price": order["price"],
                    "Lineitem compare at price": order["list_price"],
                    # The whole point of the exercise. Without this the order
                    # reaches the customer's profile and contributes nothing to
                    # demand.
                    "Lineitem sku": order["sku"],
                    "Lineitem variant title": order["variant"],
                    "Lineitem requires shipping": "TRUE",
                    "Lineitem taxable": "TRUE",
                    "Billing Name": person.name,
                    "Billing City": "Riyadh",
                    "Billing Country": "SA",
                    "Billing Phone": person.phone,
                    "Vendor": order["brand"],
                })
    return orders, number - 200_000


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="./shopify")
    parser.add_argument("--customers", type=int, default=180)
    parser.add_argument(
        "--brand", default="Aleena",
        help="only customers who buy this brand; blank for all of them",
    )
    parser.add_argument(
        "--safe-domains", action="store_true",
        help="rewrite every address to @example.com, which cannot deliver",
    )
    args = parser.parse_args()

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    products = write_products(out / "products.csv")
    customers = write_customers(
        out / "customers.csv",
        count=args.customers,
        safe=args.safe_domains,
        brand=args.brand or None,
    )
    orders, _ = write_orders(
        out / "orders.csv", count=args.customers, safe=args.safe_domains
    )
    print(f"{out / 'products.csv'}: {products} variant row(s) across {len(CATALOGUE)} products")
    print(f"{out / 'customers.csv'}: {customers} customer(s)")
    print(f"{out / 'orders.csv'}: {orders} order(s)")
    if not args.safe_domains:
        print(
            "\nthe addresses are invented at real domains and may belong to somebody. "
            "Marketing acceptance is 'no' on every row; pass --safe-domains to rewrite "
            "them to @example.com."
        )


# Shopify's own importer takes products and customers but *not* orders — those
# go in through the Admin API or an app such as Matrixify, which reads the column
# order written here. Said plainly rather than discovered at the import screen.
if __name__ == "__main__":
    main()
