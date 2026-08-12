"""The claims, measured rather than asserted.

Each test builds a situation whose right answer is obvious by hand, so a wrong
figure on the console can be traced to a rule rather than to arithmetic.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from cdp.models import (
    ActivationRun,
    Event,
    Identifier,
    IdentityMerge,
    MergeReview,
    Person,
    Segment,
)
from cdp.proof.service import ProofService

NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)


async def _person(session, *, merged_into: str | None = None) -> str:
    person = Person(merged_into_id=merged_into)
    session.add(person)
    await session.flush()
    return person.id


async def _identifier(session, person_id, *, kind, value, context="checkout") -> None:
    session.add(
        Identifier(
            person_id=person_id,
            kind=kind,
            value=value,
            first_seen_at=NOW - timedelta(days=1),
            last_seen_at=NOW,
            capture_context=context,
        )
    )
    await session.flush()


async def _order(session, person_id, amount: str) -> None:
    session.add(
        Event(
            person_id=person_id,
            source="shopify",
            name="order_paid",
            occurred_at=NOW,
            value_amount=Decimal(amount),
            currency="SAR",
            payload={},
        )
    )
    await session.flush()


async def test_merged_people_are_not_counted_twice(session):
    """A merged-away person keeps its row so old ids still resolve. Counting it
    would understate exactly the work this measurement exists to show."""
    winner = await _person(session)
    await _person(session, merged_into=winner)
    await _identifier(session, winner, kind="email", value="latifa@example.com")
    await _identifier(session, winner, kind="phone", value="+966500000001")
    session.add(
        IdentityMerge(
            winner_person_id=winner,
            loser_person_id=winner,
            linked_by_kind="email",
            linked_by_value="latifa@example.com",
            reason="strong_link",
        )
    )
    await session.flush()

    stitching = await ProofService(session).stitching()

    assert (stitching.people, stitching.identifiers) == (1, 2)
    assert stitching.identifiers_per_person == 2.0
    assert stitching.merges == 1


async def test_a_cart_token_does_not_make_an_order_attributed(session):
    """Anonymous revenue is real revenue, and reporting it as known would inflate
    the only number that justifies the project."""
    known = await _person(session)
    await _identifier(session, known, kind="email", value="latifa@example.com")
    await _order(session, known, "300.00")

    walk_in = await _person(session)
    await _identifier(session, walk_in, kind="cart_token", value="cart-token-9001")
    await _order(session, walk_in, "100.00")

    attribution = await ProofService(session).attribution()

    assert attribution.total_amount == Decimal("400.00")
    assert attribution.known_amount == Decimal("300.00")
    assert (attribution.known_orders, attribution.total_orders) == (1, 2)
    assert attribution.share == 0.75
    assert attribution.currency == "SAR"


async def test_an_empty_database_reports_zero_rather_than_failing(session):
    """The console renders before the first webhook arrives. A division by zero
    here would make the whole page look broken on day one."""
    result = await ProofService(session).collect()

    assert result.stitching.identifiers_per_person == 0.0
    assert result.attribution.share == 0.0
    assert result.attribution.total_amount == Decimal(0)
    assert result.refusals.delivered == 0


async def test_refusals_count_the_two_reasons_apart(session):
    """One is answered by asking her, the other by getting an identifier she has
    confirmed is hers. A single 'skipped' total would hide which."""
    segment = Segment(key="everyone", name="Everyone", definition={}, brand="rawash")
    session.add(segment)
    await session.flush()
    session.add(
        ActivationRun(
            segment_id=segment.id,
            destination="mock",
            requested=10,
            delivered=7,
            skipped_no_consent=2,
            skipped_identifier_risk=1,
        )
    )
    person = await _person(session)
    await _identifier(session, person, kind="phone", value="+966500000009", context="activation")

    refusals = await ProofService(session).refusals()

    assert (refusals.delivered, refusals.skipped_no_consent) == (7, 2)
    assert refusals.skipped_identifier_risk == 1
    assert refusals.risky_identifiers == 1


async def test_open_reviews_are_surfaced(session):
    """The family-iPad queue is work waiting for a human. Left off the page it
    grows unnoticed, and every case in it is a customer whose records are split."""
    a, b = await _person(session), await _person(session)
    session.add(
        MergeReview(
            person_a_id=a,
            person_b_id=b,
            linked_by_kind="device_id",
            linked_by_value="device-1",
            reason="weak_link_between_identified_people",
        )
    )
    await session.flush()

    assert (await ProofService(session).stitching()).open_reviews == 1


async def test_the_endpoint_serves_money_as_a_string(session, client):
    """A float would round a riyal total in a way nobody can reproduce, and this
    figure is meant to be checkable against the store's own reports."""
    person = await _person(session)
    await _identifier(session, person, kind="email", value="latifa@example.com")
    await _order(session, person, "1234.56")
    await session.commit()

    response = await client.get("/proof")

    assert response.status_code == 200
    body = response.json()
    assert body["attribution"]["known_amount"] == "1234.56"
    assert body["attribution"]["share"] == 1.0


async def test_the_endpoint_needs_the_api_key(client):
    client.headers.pop("X-API-Key")
    assert (await client.get("/proof")).status_code == 401
