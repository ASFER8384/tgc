"""Finding one customer among many.

The list is not a database dump. Somebody is on the phone giving a name or the
number she is calling from, and the console has to land on her — which is the
whole point of resolution, since she may have given the email once and the phone
another time.
"""

from tests.cdp.conftest import SHOPIFY_SECRET
from tests.cdp.factories import shopify_order, signed


async def ingest(client, payload: dict) -> dict:
    body, mac = signed(SHOPIFY_SECRET, payload)
    response = await client.post(
        "/ingest/shopify",
        content=body,
        headers={
            "X-Shopify-Topic": "orders/paid",
            "X-Shopify-Hmac-Sha256": mac,
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _three_customers(client) -> None:
    await ingest(
        client,
        shopify_order(
            order_id=8001,
            email="noura@example.com",
            phone="+966 50 111 1111",
            customer_id=9101,
            first_name="Noura",
            last_name="Al Qahtani",
            total="100.00",
            lines=[("Aleena", "100.00", 1)],
        ),
    )
    await ingest(
        client,
        shopify_order(
            order_id=8002,
            email="latifa@example.com",
            phone="+966 50 222 2222",
            customer_id=9102,
            first_name="Latifa",
            last_name="Al Shammari",
            total="900.00",
            lines=[("Rawash", "900.00", 1)],
        ),
    )
    await ingest(
        client,
        shopify_order(
            order_id=8003,
            email="maha@example.com",
            phone="+966 50 333 3333",
            customer_id=9103,
            first_name="Maha",
            last_name="Al Otaibi",
            total="500.00",
            lines=[("Aynola", "500.00", 1)],
        ),
    )


async def test_the_biggest_customers_come_first(client) -> None:
    """Ordered in the database, not after the fetch. Ranking a page that was
    itself chosen arbitrarily would push the best customers off the end as soon
    as there are more of them than the limit."""
    await _three_customers(client)

    page = (await client.get("/persons")).json()

    assert page["total"] == 3
    assert [r["display_name"] for r in page["people"]] == [
        "Latifa Al Shammari",
        "Maha Al Otaibi",
        "Noura Al Qahtani",
    ]


async def test_the_limit_keeps_the_biggest_rather_than_any_three(client) -> None:
    await _three_customers(client)

    page = (await client.get("/persons?limit=1")).json()

    assert [r["display_name"] for r in page["people"]] == ["Latifa Al Shammari"]
    # The page is one row; the count is of everybody the filter matched. A total
    # that reported the page size would make the pager say there is one customer.
    assert page["total"] == 3


async def test_she_is_found_by_her_phone_number(client) -> None:
    """The number she is calling from, not the one on the account she used."""
    await _three_customers(client)

    page = (await client.get("/persons?q=502222222")).json()

    assert [r["display_name"] for r in page["people"]] == ["Latifa Al Shammari"]
    assert page["total"] == 1, "the count follows the search, not the whole list"


async def test_she_is_found_by_her_email(client) -> None:
    await _three_customers(client)

    page = (await client.get("/persons?q=MAHA@EXAMPLE")).json()

    assert [r["display_name"] for r in page["people"]] == ["Maha Al Otaibi"]
    assert page["total"] == 1, "the count follows the search, not the whole list"


async def test_she_is_found_by_part_of_her_name(client) -> None:
    await _three_customers(client)

    page = (await client.get("/persons?q=qahtani")).json()

    assert [r["display_name"] for r in page["people"]] == ["Noura Al Qahtani"]
    assert page["total"] == 1, "the count follows the search, not the whole list"


async def test_a_search_that_matches_nobody_is_empty_not_everybody(client) -> None:
    """An unmatched filter that silently returns the whole list is how somebody
    ends up reading the wrong customer's history aloud."""
    await _three_customers(client)

    page = (await client.get("/persons?q=zzzzz")).json()
    assert page["people"] == []
    assert page["total"] == 0


async def test_a_second_page_holds_the_customers_the_first_one_did_not(client) -> None:
    """Paging is cut in the database now, so the two halves have to add up to the
    whole and share nobody. Ordered by lifetime value, which ties — the person id
    goes with it as a tiebreak, or the same customer appears on both pages and
    another appears on neither."""
    await _three_customers(client)

    first = (await client.get("/persons?limit=2&offset=0")).json()
    second = (await client.get("/persons?limit=2&offset=2")).json()

    assert first["total"] == second["total"] == 3
    assert len(first["people"]) == 2
    assert len(second["people"]) == 1
    names = [r["display_name"] for r in first["people"] + second["people"]]
    assert names == ["Latifa Al Shammari", "Maha Al Otaibi", "Noura Al Qahtani"]


async def test_a_page_past_the_end_is_empty_rather_than_the_last_one_again(client) -> None:
    """An offset beyond the list has to come back empty. Wrapping round to the
    start would show the biggest customers under a heading saying page nine."""
    await _three_customers(client)

    page = (await client.get("/persons?limit=2&offset=99")).json()

    assert page["people"] == []
    assert page["total"] == 3, "there are still three of them"
