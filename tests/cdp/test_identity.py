from datetime import UTC, datetime

from sqlalchemy import func, select

from cdp.identity.service import IdentifierIn, IdentityService
from cdp.models import Identifier, MergeReview, Person

SEEN = datetime(2026, 7, 14, 10, 0, tzinfo=UTC)


async def resolve(session, mapping: dict[str, str | None], *, seen_at=SEEN):
    service = IdentityService(session)
    return await service.resolve(service.prepare(mapping), seen_at=seen_at)


async def test_same_person_across_formats_is_one_profile(session) -> None:
    first = await resolve(session, {"email": "Noura@Gmail.com", "phone": "+966 50 123 4567"})
    second = await resolve(session, {"phone": "0501234567"})
    assert second.person_id == first.person_id
    assert await session.scalar(select(func.count()).select_from(Person)) == 1


async def test_two_strangers_stay_separate(session) -> None:
    a = await resolve(session, {"phone": "0501234567"})
    b = await resolve(session, {"phone": "0509999999"})
    assert a.person_id != b.person_id


async def test_event_carrying_both_identities_merges_them(session) -> None:
    # She ordered online with her email; the store logged her phone at the till.
    online = await resolve(session, {"email": "noura@gmail.com"})
    instore = await resolve(session, {"phone": "0501234567"})
    assert online.person_id != instore.person_id

    # Checkout carries both — first-party evidence from the source system.
    joined = await resolve(session, {"email": "noura@gmail.com", "phone": "0501234567"})
    assert joined.merged_person_ids
    assert joined.person_id in {online.person_id, instore.person_id}

    survivors = await session.scalars(select(Person).where(Person.merged_into_id.is_(None)))
    assert len(list(survivors)) == 1


async def test_merge_keeps_the_old_id_resolvable(session) -> None:
    online = await resolve(session, {"email": "noura@gmail.com"})
    instore = await resolve(session, {"phone": "0501234567"})
    joined = await resolve(session, {"email": "noura@gmail.com", "phone": "0501234567"})

    service = IdentityService(session)
    # Downstream systems hold ids we no longer consider canonical; they must not 404.
    for old in (online.person_id, instore.person_id):
        assert await service.canonical_id(old) == joined.person_id


async def test_shared_device_between_two_identified_people_is_queued_not_merged(session) -> None:
    # The family-iPad case: guessing here would show one customer another
    # customer's order history.
    mother = await resolve(session, {"email": "noura@gmail.com", "device_id": "ipad-7"})
    daughter_first = await resolve(session, {"email": "sara@outlook.com"})
    daughter = await resolve(session, {"email": "sara@outlook.com", "device_id": "ipad-7"})

    assert daughter.person_id == daughter_first.person_id
    assert daughter.person_id != mother.person_id
    assert daughter.review_ids, "expected a human review, not a silent merge"

    reviews = list(await session.scalars(select(MergeReview)))
    assert len(reviews) == 1
    assert reviews[0].status == "open"
    assert reviews[0].linked_by_kind == "device_id"


async def test_new_email_on_someone_elses_device_does_not_graft_onto_them(session) -> None:
    """The variant that a fixture bug once hid: only the *device* matches an
    existing person, and the event brings a brand-new strong identifier. Attaching
    it would hand a second woman the first one's entire order history."""
    mother = await resolve(session, {"email": "noura@gmail.com", "device_id": "ipad-7"})
    daughter = await resolve(session, {"email": "brand.new@outlook.com", "device_id": "ipad-7"})

    assert daughter.person_id != mother.person_id
    assert daughter.review_ids

    # The shared device stays with its original owner rather than being stolen.
    device = await session.scalar(select(Identifier).where(Identifier.kind == "device_id"))
    assert device.person_id == mother.person_id


async def test_anonymous_device_is_claimed_by_the_person_who_identifies(session) -> None:
    anonymous = await resolve(session, {"device_id": "browser-42"})
    identified = await resolve(session, {"device_id": "browser-42", "email": "noura@gmail.com"})
    # Nothing can be exposed by claiming a browser that belongs to nobody yet.
    assert identified.person_id == anonymous.person_id
    assert not identified.review_ids


async def test_identifier_belongs_to_exactly_one_person(session) -> None:
    await resolve(session, {"email": "noura@gmail.com", "phone": "0501234567"})
    await resolve(session, {"email": "noura@gmail.com"})
    rows = list(await session.scalars(select(Identifier).where(Identifier.kind == "email")))
    assert len(rows) == 1


async def test_resolution_without_identifiers_still_yields_a_person(session) -> None:
    # An anonymous page view is data too; it just cannot be joined yet.
    result = await IdentityService(session).resolve([], seen_at=SEEN)
    assert result.created is True
    assert result.person_id


async def test_first_and_last_seen_track_the_extremes(session) -> None:
    later = datetime(2026, 8, 1, tzinfo=UTC)
    earlier = datetime(2026, 6, 1, tzinfo=UTC)
    await resolve(session, {"email": "noura@gmail.com"}, seen_at=SEEN)
    await resolve(session, {"email": "noura@gmail.com"}, seen_at=later)
    await resolve(session, {"email": "noura@gmail.com"}, seen_at=earlier)

    row = await session.scalar(select(Identifier).where(Identifier.kind == "email"))
    assert row.first_seen_at == earlier
    assert row.last_seen_at == later


async def test_prepare_drops_junk_and_deduplicates(session) -> None:
    service = IdentityService(session)
    prepared = service.prepare(
        {"email": "  NOURA@gmail.com ", "phone": "abc", "device_id": None}
    )
    assert prepared == [IdentifierIn("email", "noura@gmail.com")]
