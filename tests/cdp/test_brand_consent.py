"""Consent is granted to a brand, not to the company.

The case these pin: she buys from Aleena and agrees Aleena may message her, then
buys from Rawash and agrees Rawash may message her. Resolution proves she is one
person — correctly. What she never agreed to is a profile spanning both, and an
audience built from one brand's behaviour to serve another.

Written adversarially, as Section V.C of the framework asks: each test issues a
request that *should* be refused and checks the data is genuinely absent, not
merely filtered out of the response afterwards.
"""

from datetime import UTC, datetime, timedelta

import pytest

from cdp.consent.service import ConsentError, ConsentService
from cdp.models import ConsentEvent, Person, PersonBrandStat, ProfileTraits
from cdp.segments.compiler import SegmentDefinitionError, compile_segment
from cdp.segments.service import SegmentService

NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)

ALEENA_BUYERS = {"all": [{"brand_purchased": "aleena"}]}
CROSS_BRAND = {"all": [{"brand_purchased": "aleena"}, {"brand_not_purchased": "rawash"}]}
# Reads no brand's behaviour, so only the messaging grant is in play. Needed to
# test the two gates separately — any definition naming a brand trips both.
ANY_BUYER = {"trait": "order_count", "op": "gte", "value": 1}


async def _shopper(session, *, brands: tuple[str, ...], grants: dict[str, list[str]]) -> str:
    """A person who bought from `brands` and granted whatsapp per brand in `grants`."""
    person = Person(display_name="Noura")
    session.add(person)
    await session.flush()
    session.add(ProfileTraits(person_id=person.id, order_count=2, computed_at=NOW))
    for brand in brands:
        session.add(PersonBrandStat(person_id=person.id, brand=brand, orders=1, spend=500))
    for brand, purposes in grants.items():
        for purpose in purposes:
            session.add(
                ConsentEvent(
                    person_id=person.id,
                    brand=brand,
                    purpose=purpose,
                    granted=True,
                    source="shopify_checkout",
                    occurred_at=NOW - timedelta(days=1),
                )
            )
    await session.flush()
    return person.id


async def _members(session, definition, consent, brand):
    return list(await session.scalars(compile_segment(definition, consent, brand=brand)))


async def test_a_grant_to_one_brand_does_not_serve_another(session):
    """The whole argument, in one test."""
    person = await _shopper(
        session, brands=("aleena",), grants={"aleena": ["marketing_whatsapp"]}
    )

    assert await _members(session, ALEENA_BUYERS, "marketing_whatsapp", "aleena") == [person]
    # Rawash has no grant from her. It gets nobody, not a filtered list.
    assert await _members(session, ALEENA_BUYERS, "marketing_whatsapp", "rawash") == []


async def test_each_brand_sees_only_its_own_grant(session):
    person = await _shopper(
        session,
        brands=("aleena", "rawash"),
        grants={"aleena": ["marketing_whatsapp"], "rawash": ["marketing_email"]},
    )

    assert await _members(session, ANY_BUYER, "marketing_whatsapp", "aleena") == [person]
    # She said yes to Rawash by email, so Rawash may not use WhatsApp.
    assert await _members(session, ANY_BUYER, "marketing_whatsapp", "rawash") == []
    assert await _members(session, ANY_BUYER, "marketing_email", "rawash") == [person]
    # And Aleena, which she gave WhatsApp to, may not mail her.
    assert await _members(session, ANY_BUYER, "marketing_email", "aleena") == []


async def test_cross_brand_audience_needs_its_own_grant(session):
    """Aleena reading her Rawash history is a separate permission from Aleena
    messaging her, and having the second does not imply the first."""
    person = await _shopper(
        session, brands=("aleena",), grants={"aleena": ["marketing_whatsapp"]}
    )

    assert await _members(session, CROSS_BRAND, "marketing_whatsapp", "aleena") == []

    session.add(
        ConsentEvent(
            person_id=person,
            brand="aleena",
            purpose="cross_brand_profiling",
            granted=True,
            source="shopify_checkout",
            occurred_at=NOW,
        )
    )
    await session.flush()

    assert await _members(session, CROSS_BRAND, "marketing_whatsapp", "aleena") == [person]


async def test_withdrawal_takes_effect_on_the_next_evaluation(session):
    person = await _shopper(
        session, brands=("aleena",), grants={"aleena": ["marketing_whatsapp"]}
    )
    assert await _members(session, ALEENA_BUYERS, "marketing_whatsapp", "aleena") == [person]

    await ConsentService(session).record(
        person, "marketing_whatsapp", False, brand="aleena", source="unsubscribe_link"
    )

    assert await _members(session, ALEENA_BUYERS, "marketing_whatsapp", "aleena") == []


async def test_withdrawing_from_one_brand_leaves_the_other_standing(session):
    """The framework's third open question, answered explicitly: unsubscribing
    from Rawash stops Rawash, and says nothing about Aleena."""
    person = await _shopper(
        session,
        brands=("aleena", "rawash"),
        grants={"aleena": ["marketing_whatsapp"], "rawash": ["marketing_whatsapp"]},
    )

    await ConsentService(session).record(
        person, "marketing_whatsapp", False, brand="rawash", source="unsubscribe_link"
    )

    assert await _members(session, ALEENA_BUYERS, "marketing_whatsapp", "aleena") == [person]
    assert await _members(session, ALEENA_BUYERS, "marketing_whatsapp", "rawash") == []


async def test_a_grant_with_no_brand_authorises_nobody(session):
    """Legacy rows the migration could not attribute. They must not act as a
    wildcard — an agreement nobody can trace to a brand is not one any brand
    may rely on."""
    person = Person(display_name="Unattributed")
    session.add(person)
    await session.flush()
    session.add(ProfileTraits(person_id=person.id, order_count=1, computed_at=NOW))
    session.add(PersonBrandStat(person_id=person.id, brand="aleena", orders=1, spend=100))
    session.add(
        ConsentEvent(
            person_id=person.id,
            brand=None,
            purpose="marketing_whatsapp",
            granted=True,
            source="legacy",
            occurred_at=NOW,
        )
    )
    await session.flush()

    for brand in ("aleena", "rawash", "aynola"):
        assert await _members(session, ALEENA_BUYERS, "marketing_whatsapp", brand) == []


async def test_an_unbranded_request_is_refused_not_answered_emptily(session):
    """"Nobody matched" and "that question cannot be asked" are different
    answers, and returning the first for the second hides the refusal."""
    with pytest.raises(SegmentDefinitionError, match="asking brand must be named"):
        compile_segment(ANY_BUYER, "marketing_whatsapp", brand=None)

    with pytest.raises(SegmentDefinitionError, match="must name the brand"):
        compile_segment(CROSS_BRAND, None, brand=None)


async def test_a_segment_that_could_never_be_evaluated_cannot_be_saved(session):
    with pytest.raises(SegmentDefinitionError):
        await SegmentService(session).upsert(
            "unscoped", "Unscoped", ALEENA_BUYERS, required_consent="marketing_whatsapp"
        )


async def test_consent_cannot_be_recorded_against_an_unknown_brand(session):
    person = await _shopper(session, brands=("aleena",), grants={})
    with pytest.raises(ConsentError, match="unknown brand"):
        await ConsentService(session).record(
            person, "marketing_whatsapp", True, brand="tgc", source="console"
        )


async def test_current_reports_brand_by_brand(session):
    person = await _shopper(
        session,
        brands=("aleena", "rawash"),
        grants={"aleena": ["marketing_whatsapp"], "rawash": ["personalization"]},
    )

    table = await ConsentService(session).current(person)

    assert table["aleena"]["marketing_whatsapp"] is True
    assert table["rawash"]["marketing_whatsapp"] is False
    assert table["rawash"]["personalization"] is True
    # A brand she has never dealt with is present and false, not missing.
    assert table["aynola"]["marketing_whatsapp"] is False
