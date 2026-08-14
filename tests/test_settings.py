"""Buying policy changed from the console, and the floor in units it introduces.

Two things worth pinning down. The first is that a setting saved through the API
actually governs the next decision, rather than being stored somewhere the
planner never reads — the failure mode is a settings page that appears to work
and changes nothing, which is worse than no page at all.

The second is the minimum stock rule, whose whole reason for existing is the
case cover cannot speak to: an item with no forecast and no sales history. Those
produce no suggestion at all today, correctly, and the floor is what lets a
buyer say "never fewer than fifty" about one anyway.
"""

import pytest

CREDENTIAL_KEYS = ("api_key", "database_url", "mail_smtp_password", "console_auto_connect")


async def _supplier_and_item(client, supplier_payload, **item):
    always_open = supplier_payload | {
        "working_days": "1,2,3,4,5,6,7", "work_start_hour": 0, "work_end_hour": 24,
    }
    supplier = (await client.post("/suppliers", json=always_open)).json()
    payload = {
        "sku": "ALN-ABAYA-BLK", "name": "Black abaya", "supplier_id": supplier["id"],
        "category": "finished_goods", "brand": "aleena", "unit": "pcs",
        "moq": 1, "pack_size": 1, "unit_cost": "120.00",
    } | item
    await client.post("/items", json=payload)
    return supplier


# ------------------------------------------------------------------- the page
async def test_settings_are_readable_and_say_where_each_value_came_from(client):
    body = (await client.get("/settings")).json()
    fields = {f["key"]: f for group in body["groups"] for f in group["fields"]}

    assert "approval_threshold_sar" in fields
    assert fields["approval_threshold_sar"]["source"] == "environment"
    assert fields["approval_threshold_sar"]["env_var"] == "SCA_APPROVAL_THRESHOLD_SAR"
    # Nothing that would let a browser lock the service out of its own data.
    assert not set(fields) & set(CREDENTIAL_KEYS)


async def test_saving_a_setting_changes_where_the_value_comes_from(client):
    response = await client.put("/settings", json={"values": {"approval_threshold_sar": 40000}})
    body = response.json()
    fields = {f["key"]: f for group in body["groups"] for f in group["fields"]}
    assert fields["approval_threshold_sar"]["value"] == 40000
    assert fields["approval_threshold_sar"]["source"] == "set here"
    assert body["changed"] == [
        {"key": "approval_threshold_sar", "from": "25000.0", "to": "40000.0"}
    ]

    # And it survives being read back by a different request, which is the whole
    # difference between this and a form that only redraws itself.
    again = (await client.get("/settings")).json()
    assert {f["key"]: f["value"] for g in again["groups"] for f in g["fields"]}[
        "approval_threshold_sar"
    ] == 40000


async def test_resetting_hands_the_setting_back_to_the_environment(client):
    await client.put("/settings", json={"values": {"ack_reminder_hours": 6}})
    body = (await client.delete("/settings/ack_reminder_hours")).json()
    field = {f["key"]: f for g in body["groups"] for f in g["fields"]}["ack_reminder_hours"]
    assert field["source"] == "environment"
    assert field["value"] == 24


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("reorder_cover_weeks", -1),          # below its own floor
        ("ack_reminder_hours", 2.5),          # not a whole number of hours
        ("home_timezone", "Mars/Olympus"),    # not a real zone
        ("approval_threshold_sar", "lots"),   # not a number at all
        ("api_key", "let-me-in"),             # not a setting anyone may change
    ],
)
async def test_a_value_that_would_not_be_safe_is_refused(client, key, value):
    response = await client.put("/settings", json={"values": {key: value}})
    assert response.status_code == 422
    # The key travels with the message so the console can put it beside the box
    # the person typed in.
    assert response.json()["detail"]["key"] == key


async def test_target_cover_must_exceed_the_reorder_point(client):
    response = await client.put(
        "/settings", json={"values": {"reorder_cover_weeks": 8, "target_cover_weeks": 4}}
    )
    assert response.status_code == 422
    assert response.json()["detail"]["key"] == "target_cover_weeks"
    # Nothing half-applied: the pair only makes sense together.
    fields = {f["key"]: f for g in (await client.get("/settings")).json()["groups"]
              for f in g["fields"]}
    assert fields["reorder_cover_weeks"]["source"] == "environment"


async def test_a_saved_threshold_governs_the_next_order(client, supplier_payload):
    supplier = await _supplier_and_item(client, supplier_payload)
    lines = [{"sku": "ALN-ABAYA-BLK", "quantity": 100, "unit_price": "120.00"}]

    # 12,000 is under the 25,000 the environment sets, so nothing gates it — bar
    # the second rule, which is not configurable: a supplier who has never
    # completed an order needs approval whatever the value.
    first = (await client.post(
        "/purchase-orders", json={"supplier_id": supplier["id"], "lines": lines}
    )).json()
    assert "first completed order" in (first["approval_reason"] or "")

    await client.put("/settings", json={"values": {"approval_threshold_sar": 5000}})
    second = (await client.post(
        "/purchase-orders", json={"supplier_id": supplier["id"], "lines": lines}
    )).json()
    # Now the value gate bites too, and it names the figure that was just saved.
    assert "5000" in second["approval_reason"]


# ------------------------------------------------------------ the units floor
async def test_an_item_below_its_minimum_is_suggested_with_no_forecast_at_all(
    client, supplier_payload
):
    """The case cover cannot answer, and the reason this rule exists."""
    await _supplier_and_item(client, supplier_payload, min_stock=50)
    await client.post("/stock", json={
        "sku": "ALN-ABAYA-BLK", "on_hand": 12, "on_order": 0, "weekly_forecast": "0",
    })
    body = (await client.post("/planning/suggest")).json()
    assert body["count"] == 1
    line = body["by_supplier"][0]["lines"][0]
    # Up to the floor and no further: there is no demand figure to buy cover
    # against, so inventing one would be exactly what this system refuses to do.
    assert line["suggest_quantity"] == 38
    assert line["below_minimum"] is True
    assert line["minimum"] == 50
    # No cover, rather than nought weeks of it.
    assert line["weeks_cover"] is None
    assert line["forecast_source"] == "minimum"
    assert "12 available against a minimum of 50" in line["reason"]


async def test_without_a_minimum_the_same_item_produces_nothing(client, supplier_payload):
    await _supplier_and_item(client, supplier_payload)
    await client.post("/stock", json={
        "sku": "ALN-ABAYA-BLK", "on_hand": 12, "on_order": 0, "weekly_forecast": "0",
    })
    assert (await client.post("/planning/suggest")).json()["count"] == 0


async def test_a_floor_is_not_overruled_by_healthy_cover(client, supplier_payload):
    """Somebody stating a minimum outranks the system's estimate of one."""
    await _supplier_and_item(client, supplier_payload, min_stock=500)
    # Forty weeks of cover by the forecast, and still under the floor.
    await client.post("/stock", json={
        "sku": "ALN-ABAYA-BLK", "on_hand": 400, "on_order": 0, "weekly_forecast": "10",
    })
    line = (await client.post("/planning/suggest")).json()["by_supplier"][0]["lines"][0]
    assert line["below_minimum"] is True
    assert line["suggest_quantity"] == 100


async def test_cover_wins_where_it_asks_for_more_than_the_floor(client, supplier_payload):
    """A line tripping both wants the larger of the two, or it triggers again
    next week having bought only enough to clear the minimum."""
    await _supplier_and_item(client, supplier_payload, min_stock=50)
    await client.post("/stock", json={
        "sku": "ALN-ABAYA-BLK", "on_hand": 40, "on_order": 0, "weekly_forecast": "30",
    })
    line = (await client.post("/planning/suggest")).json()["by_supplier"][0]["lines"][0]
    # Eight weeks at thirty a week is 240, less 40 on hand — well past the floor.
    assert line["suggest_quantity"] == 200
    assert line["below_minimum"] is True


async def test_a_zero_on_the_item_turns_the_global_default_off_for_that_line(
    client, supplier_payload
):
    """Blank inherits, zero decides. Collapsing the two would make it impossible
    to exempt a single line from a floor set across the catalogue."""
    await _supplier_and_item(client, supplier_payload, min_stock=0)
    await client.put("/settings", json={"values": {"min_stock_default": 100}})
    await client.post("/stock", json={
        "sku": "ALN-ABAYA-BLK", "on_hand": 12, "on_order": 0, "weekly_forecast": "0",
    })
    assert (await client.post("/planning/suggest")).json()["count"] == 0


async def test_policy_is_editable_per_item_without_touching_the_rest_of_it(
    client, supplier_payload
):
    """The settings page edits thresholds across the catalogue, so it sends them
    together. It must not be able to reset a pack size on the way past."""
    await _supplier_and_item(client, supplier_payload, moq=100, pack_size=25, unit_cost="120.00")

    body = (await client.put("/items/policy", json={"values": {"ALN-ABAYA-BLK": {
        "min_stock": 50, "reorder_cover_weeks": 2, "target_cover_weeks": 6,
        "demand_window_weeks": 4,
    }}})).json()
    assert {c["field"] for c in body["changed"]} == {
        "min_stock", "reorder_cover_weeks", "target_cover_weeks", "demand_window_weeks"
    }

    item = (await client.get("/items")).json()[0]
    assert item["policy"]["reorder_cover_weeks"] == {"own": 2.0, "in_force": 2.0}
    # Untouched: still inheriting, and the global is what is in force.
    assert item["min_stock"] == 50
    # Everything the route was not asked about is where it was.
    assert (item["moq"], item["pack_size"], item["unit_cost"]) == (100, 25, "120.00")

    # Null hands the item back to the global default; it is not the same as zero.
    back = (await client.put("/items/policy", json={"values": {"ALN-ABAYA-BLK": {}}})).json()
    assert {c["field"]: c["to"] for c in back["changed"]} == {
        "min_stock": None, "reorder_cover_weeks": None,
        "target_cover_weeks": None, "demand_window_weeks": None,
    }
    after = (await client.get("/items")).json()[0]
    assert after["policy"]["reorder_cover_weeks"] == {"own": None, "in_force": 4.0}


async def test_an_item_threshold_is_held_to_the_same_bounds_as_the_global_one(
    client, supplier_payload
):
    await _supplier_and_item(client, supplier_payload)
    response = await client.put(
        "/items/policy", json={"values": {"ALN-ABAYA-BLK": {"reorder_cover_weeks": -3}}}
    )
    assert response.status_code == 422
    assert "ALN-ABAYA-BLK" in response.json()["detail"]

    # And the pair is cross-checked against what the item will actually run
    # under, not only against what this request carried.
    clash = await client.put(
        "/items/policy", json={"values": {"ALN-ABAYA-BLK": {"target_cover_weeks": 2}}}
    )
    assert clash.status_code == 422
    assert "reorder below" in clash.json()["detail"]


async def test_an_items_own_thresholds_drive_its_suggestion(client, supplier_payload):
    """The point of the per item table: this line is bought on its own rule."""
    await _supplier_and_item(client, supplier_payload)
    # Six weeks of cover against a global reorder point of four: nothing to do.
    await client.post("/stock", json={
        "sku": "ALN-ABAYA-BLK", "on_hand": 60, "on_order": 0, "weekly_forecast": "10",
    })
    assert (await client.post("/planning/suggest")).json()["count"] == 0

    # Unless this item says it wants eight weeks and buys up to twelve.
    await client.put("/items/policy", json={"values": {"ALN-ABAYA-BLK": {
        "reorder_cover_weeks": 8, "target_cover_weeks": 12,
    }}})
    body = (await client.post("/planning/suggest")).json()
    assert body["count"] == 1
    # Twelve weeks at ten a week is 120, less the 60 already there.
    assert body["by_supplier"][0]["lines"][0]["suggest_quantity"] == 60


async def test_policy_for_an_unknown_item_is_refused(client):
    response = await client.put("/items/policy", json={"values": {"NOPE-1": {"min_stock": 10}}})
    assert response.status_code == 422
    assert "NOPE-1" in response.json()["detail"]


async def test_the_global_default_covers_items_that_set_none_of_their_own(
    client, supplier_payload
):
    await _supplier_and_item(client, supplier_payload)
    await client.post("/stock", json={
        "sku": "ALN-ABAYA-BLK", "on_hand": 12, "on_order": 0, "weekly_forecast": "0",
    })
    assert (await client.post("/planning/suggest")).json()["count"] == 0

    await client.put("/settings", json={"values": {"min_stock_default": 100}})
    body = (await client.post("/planning/suggest")).json()
    assert body["count"] == 1
    assert body["by_supplier"][0]["lines"][0]["suggest_quantity"] == 88
    # And the catalogue says which figure is being applied and where it came from.
    item = (await client.get("/items")).json()[0]
    assert item["min_stock"] is None
    assert item["min_stock_effective"] == 100
    assert item["below_minimum"] is True
