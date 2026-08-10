"""Seed a realistic supplier network across four time zones.

Run against a live API so it exercises the same path a real connector does. The
suppliers are chosen for their clocks as much as their goods: a Guangzhou mill
with a two hour overlap with Riyadh, an Istanbul packer with six, a Mumbai
component maker, and a local Riyadh printer with a full working day of overlap.
That spread is the entire problem the module exists to solve.

    .venv/Scripts/python -m scripts.seed_demo --base-url http://localhost:8001
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime, timedelta

import httpx

SUPPLIERS = [
    {
        "code": "GZ-TEX", "name": "Guangzhou Silk Mill", "country": "CN",
        "email": "orders@gzsilkmill.cn", "timezone": "Asia/Shanghai",
        "working_days": "1,2,3,4,5,6", "work_start_hour": 8, "work_end_hour": 18,
        "lead_time_days": 42, "currency": "CNY", "min_order_value": 5000,
    },
    {
        "code": "IST-PACK", "name": "Istanbul Packaging Co", "country": "TR",
        "email": "siparis@istpack.com.tr", "timezone": "Europe/Istanbul",
        "working_days": "1,2,3,4,5", "work_start_hour": 9, "work_end_hour": 18,
        "lead_time_days": 21, "currency": "EUR", "min_order_value": 2000,
    },
    {
        "code": "MUM-COS", "name": "Mumbai Cosmetics Components", "country": "IN",
        "email": "sales@mumcoscomp.in", "timezone": "Asia/Kolkata",
        "working_days": "1,2,3,4,5,6", "work_start_hour": 10, "work_end_hour": 19,
        "lead_time_days": 28, "currency": "USD", "min_order_value": 1500,
    },
    {
        "code": "RUH-PRINT", "name": "Riyadh Print House", "country": "SA",
        "email": "info@riyadhprint.sa", "timezone": "Asia/Riyadh",
        # Sunday to Thursday, the Gulf working week. A hardcoded Monday to Friday
        # would have this supplier closed on their busiest days.
        "working_days": "7,1,2,3,4", "work_start_hour": 9, "work_end_hour": 17,
        "lead_time_days": 7, "currency": "SAR", "min_order_value": 0,
    },
]

# sku, name, supplier code, category, brand, unit, moq, pack, cost,
# on hand, on order, weekly forecast
ITEMS = [
    ("ALN-SILK-NVY", "Navy silk, 140cm", "GZ-TEX", "fabric", "aleena", "m", 300, 50, 42.00,
     260, 0, 90),
    ("ALN-SILK-BLK", "Black silk, 140cm", "GZ-TEX", "fabric", "aleena", "m", 300, 50, 42.00,
     1400, 300, 85),
    ("ALN-ABAYA-01", "Embroidered abaya", "GZ-TEX", "finished_goods", "aleena", "pcs", 100, 25,
     180.00, 120, 0, 60),
    ("RWS-LIP-TUBE", "Lipstick tube, matte black", "MUM-COS", "component", "rawash", "pcs",
     2000, 500, 3.20, 3000, 0, 1200),
    ("RWS-CARTON-S", "Small printed carton", "IST-PACK", "packaging", "rawash", "pcs", 1000,
     250, 1.10, 900, 1000, 700),
    ("AYN-BOTTLE-50", "50ml glass bottle", "IST-PACK", "packaging", "aynola", "pcs", 1000, 250,
     6.40, 4200, 0, 300),
    ("AYN-BOX-LUX", "Luxury outer box", "RUH-PRINT", "packaging", "aynola", "pcs", 500, 100,
     9.80, 180, 0, 220),
]

# Supplier replies, written the way suppliers actually write them: a clean
# confirmation, a delay buried in a polite paragraph, an invoice that does not
# match, and one message no rule should pretend to understand.
REPLIES = [
    (
        "GZ-TEX", "Re: {po} - Order Confirmation",
        "Dear Purchasing Team,\n\nThank you for {po}. We confirm the order and will ship on "
        "2026-09-28.\n\nBest regards,\nLily Chen",
    ),
    (
        "IST-PACK", "RE: {po}",
        "Hello,\n\nWe have received {po}. Unfortunately our carton line is fully booked this "
        "month, so this will be delayed. New ship date 12 Nov 2026.\n\nRegards,\nMehmet",
    ),
    (
        "MUM-COS", "Invoice for {po}",
        "Please find attached our invoice against {po} for USD 9,850.00. Kindly arrange "
        "payment as per agreed terms.\n\nThanks,\nRajesh",
    ),
    (
        "RUH-PRINT", "شكرا",
        "Thanks for the file, we will revert.",
    ),
]


class Seeder:
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url
        self.api_key = api_key

    async def run(self) -> None:
        async with httpx.AsyncClient(base_url=self.base_url, timeout=60) as client:
            client.headers["X-API-Key"] = self.api_key

            codes: dict[str, str] = {}
            for supplier in SUPPLIERS:
                r = await client.post("/suppliers", json=supplier)
                r.raise_for_status()
                body = r.json()
                codes[supplier["code"]] = body["id"]
                state = "open" if body["open_now"] else f"opens {body['next_open_local']}"
                print(f"  {body['name']:<32} {body['local_time']} local, {state}")

            for sku, name, code, category, brand, unit, moq, pack, cost, hand, order, fc in ITEMS:
                await client.post("/items", json={
                    "sku": sku, "name": name, "supplier_id": codes[code], "category": category,
                    "brand": brand, "unit": unit, "moq": moq, "pack_size": pack,
                    "unit_cost": str(cost),
                })
                await client.post("/stock", json={
                    "sku": sku, "on_hand": hand, "on_order": order, "weekly_forecast": str(fc),
                })

            suggested = (await client.post("/planning/suggest")).json()
            print(f"\n  {suggested['count']} lines below cover")
            for group in suggested["by_supplier"]:
                print(f"    {group['supplier']:<32} {len(group['lines'])} lines, {group['value']}")

            created = (await client.post("/planning/create-orders")).json()
            print(f"\n  {created['created']} draft orders")
            for order in created["orders"]:
                gate = order["approval_reason"] or "auto approved"
                print(
                    f"    {order['number']}  {order['total_value']:>10}  "
                    f"{order['status']:<17} {gate}"
                )

            # Approve and send everything, so the demo starts with orders in
            # flight rather than an empty board.
            numbers: dict[str, str] = {}
            for order in created["orders"]:
                number = order["number"]
                detail = (await client.get(f"/purchase-orders/{number}")).json()
                supplier_code = next(
                    code for code, sid in codes.items() if sid == detail["supplier"]["id"]
                )
                numbers[supplier_code] = number
                if detail["status"] == "pending_approval":
                    await client.post(f"/purchase-orders/{number}/approve",
                                      json={"approver": "procurement.lead"})
                sent = (await client.post(f"/purchase-orders/{number}/send")).json()
                delivery = sent["delivery"]
                outcome = "sent" if delivery["sent"] else f"queued: {delivery['reason']}"
                print(f"    {number} {outcome}")

            print("\n  supplier replies")
            now = datetime.now(UTC)
            for index, (code, subject, body) in enumerate(REPLIES):
                number = numbers.get(code)
                if not number:
                    continue
                supplier = next(s for s in SUPPLIERS if s["code"] == code)
                result = (await client.post("/inbound/email", json={
                    "external_id": f"seed-{index}-{number}",
                    "from_address": supplier["email"],
                    "subject": subject.format(po=number),
                    "body": body.format(po=number),
                    "received_at": (now - timedelta(hours=6 - index)).isoformat(),
                })).json()
                print(f"    {code:<10} read as {result['kind']:<16} "
                      f"conf {result['confidence']}  {'; '.join(result['actions'])}")

            issues = (await client.get("/issues")).json()
            print(f"\n  {len(issues)} open exceptions")
            for issue in issues:
                print(f"    [{issue['severity']:<6}] {issue['kind']:<20} {issue['detail'][:70]}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8001")
    parser.add_argument("--api-key", default="dev-key-change-me")
    args = parser.parse_args()
    asyncio.run(Seeder(args.base_url, args.api_key).run())


if __name__ == "__main__":
    main()
