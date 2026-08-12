"""A number she consented on may still not be hers.

Consent answers "may we contact this person". It does not answer "is this
number that person". A phone written on a form at a mall stand may be her
friend's; a phone on a gift order is often the recipient's. Sending her purchase
history there delivers one customer's data to another, and no later correction
undoes it.

So the capture circumstance is recorded at capture and blocks addressed
messaging on its own, whatever the consent state says.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from cdp.activation.port import MockDestination, ShopifySegmentDestination
from cdp.activation.service import ActivationService
from cdp.models import ActivationDelivery, ConsentEvent, Identifier, Person, ProfileTraits, Segment

NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
EVERYONE = {"trait": "order_count", "op": "gte", "value": 0}


async def _person(session, *, context: str, kind: str = "phone", value: str = "+966500000001"):
    person = Person(display_name="Latifa")
    session.add(person)
    await session.flush()
    session.add(ProfileTraits(person_id=person.id, order_count=1, computed_at=NOW))
    session.add(
        Identifier(
            person_id=person.id,
            kind=kind,
            value=value,
            first_seen_at=NOW - timedelta(days=10),
            last_seen_at=NOW - timedelta(days=10),
            capture_context=context,
        )
    )
    session.add(
        ConsentEvent(
            person_id=person.id,
            brand="rawash",
            purpose="marketing_whatsapp",
            granted=True,
            source="activation_form",
            occurred_at=NOW,
        )
    )
    await session.flush()
    return person.id


async def _segment(session) -> None:
    session.add(
        Segment(
            key="everyone",
            name="Everyone",
            definition=EVERYONE,
            required_consent="marketing_whatsapp",
            brand="rawash",
        )
    )
    await session.flush()


async def test_a_mall_captured_number_is_not_messaged(session):
    await _person(session, context="activation")
    await _segment(session)

    destination = MockDestination()
    run = await ActivationService(session, registry={"mock": destination}, actor="test").run(
        "everyone", "mock"
    )

    # She consented. The number may still be her friend's.
    assert (run.delivered, run.skipped_no_consent, run.skipped_identifier_risk) == (0, 0, 1)
    assert destination.delivered == []

    logged = await session.scalar(select(ActivationDelivery))
    assert logged.status == "skipped"
    assert "captured at activation" in logged.detail


async def test_a_gift_order_number_is_not_messaged(session):
    await _person(session, context="gift")
    await _segment(session)

    destination = MockDestination()
    run = await ActivationService(session, registry={"mock": destination}, actor="test").run(
        "everyone", "mock"
    )

    assert run.skipped_identifier_risk == 1


async def test_a_checkout_number_is_messaged(session):
    await _person(session, context="checkout")
    await _segment(session)

    destination = MockDestination()
    run = await ActivationService(session, registry={"mock": destination}, actor="test").run(
        "everyone", "mock"
    )

    assert (run.delivered, run.skipped_identifier_risk) == (1, 0)


async def test_an_unaddressed_destination_is_unaffected(session):
    """Tagging her own record on the store sends nothing to anybody, so a
    doubtful phone number cannot cause a disclosure through it. Blocking it too
    would be caution with no beneficiary."""
    await _person(session, context="activation")
    await _segment(session)
    session.add(
        ConsentEvent(
            person_id=(await session.scalar(select(Person.id))),
            brand="rawash",
            purpose="personalization",
            granted=True,
            source="activation_form",
            occurred_at=NOW,
        )
    )
    await session.flush()

    destination = ShopifySegmentDestination(shop=None, access_token=None)
    run = await ActivationService(
        session, registry={"shopify_segment": destination}, actor="test"
    ).run("everyone", "shopify_segment")

    # Not skipped for risk — it fails later, because it is not configured.
    assert run.skipped_identifier_risk == 0
    assert run.failed == 1


async def test_risk_is_cleared_when_she_confirms_the_number_herself(session):
    """A number first written on a mall form and later typed into a checkout by
    the customer has been confirmed by her. Better evidence clears the doubt;
    weaker evidence never adds it back."""
    from cdp.identity.service import IdentityService

    person_id = await _person(session, context="activation")
    service = IdentityService(session, actor="test")

    await service.resolve(
        service.prepare({"phone": "+966500000001"}, capture_context="checkout"),
        seen_at=NOW,
    )

    row = await session.scalar(select(Identifier).where(Identifier.person_id == person_id))
    assert row.capture_context == "checkout"
    assert row.third_party_plausible is False
