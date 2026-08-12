from datetime import UTC, datetime

from sqlalchemy import ScalarSelect, select
from sqlalchemy.ext.asyncio import AsyncSession

from cdp.models import BRANDS, PURPOSES, AuditLog, ConsentEvent, Person


class ConsentError(ValueError):
    pass


def latest_consent_subquery(
    purpose: str, person_id_column, brand: str | None = None
) -> ScalarSelect[bool]:
    """The consent gate, as a correlated scalar subquery.

    Every audience query composes this instead of trusting a denormalised flag,
    so consent state can never be stale by one sync cycle. No row at all yields
    NULL, which fails the ``is true`` test — silence is not agreement.

    When a brand is given, only that brand's grants are considered. A grant made
    to Aleena is not evidence that Rawash may write to her, and rows with no
    brand recorded satisfy nothing: an agreement nobody can attribute to a brand
    cannot be relied on by one.
    """
    query = select(ConsentEvent.granted).where(
        ConsentEvent.person_id == person_id_column, ConsentEvent.purpose == purpose
    )
    if brand is not None:
        query = query.where(ConsentEvent.brand == brand)
    return (
        query.order_by(
            ConsentEvent.occurred_at.desc(),
            ConsentEvent.created_at.desc(),
            ConsentEvent.id.desc(),
        )
        .limit(1)
        .scalar_subquery()
    )


class ConsentService:
    def __init__(self, session: AsyncSession, *, actor: str = "system"):
        self.session = session
        self.actor = actor

    async def record(
        self,
        person_id: str,
        purpose: str,
        granted: bool,
        *,
        brand: str,
        source: str,
        evidence: str | None = None,
        occurred_at: datetime | None = None,
    ) -> ConsentEvent:
        if purpose not in PURPOSES:
            raise ConsentError(f"unknown consent purpose: {purpose}")
        if brand not in BRANDS:
            raise ConsentError(f"unknown brand: {brand}")
        if await self.session.get(Person, person_id) is None:
            raise ConsentError(f"unknown person: {person_id}")

        row = ConsentEvent(
            person_id=person_id,
            brand=brand,
            purpose=purpose,
            granted=granted,
            source=source,
            evidence=evidence,
            occurred_at=occurred_at or datetime.now(UTC),
        )
        self.session.add(row)
        self.session.add(
            AuditLog(
                actor=self.actor,
                action="consent.granted" if granted else "consent.revoked",
                entity="person",
                entity_id=person_id,
                meta={"purpose": purpose, "brand": brand, "source": source},
            )
        )
        await self.session.flush()
        return row

    async def current(self, person_id: str, brand: str | None = None) -> dict:
        """Latest state per purpose.

        With a brand, the answer is that brand's alone. Without one, the answer
        is a brand-by-brand table rather than a merged summary — flattening it
        would produce exactly the company-wide permission that no customer ever
        agreed to.

        Purposes with no record are reported as False rather than omitted, so a
        caller cannot mistake absence for assent.
        """
        rows = (
            await self.session.scalars(
                select(ConsentEvent)
                .where(ConsentEvent.person_id == person_id)
                .order_by(ConsentEvent.occurred_at, ConsentEvent.created_at, ConsentEvent.id)
            )
        ).all()

        if brand is not None:
            state = dict.fromkeys(PURPOSES, False)
            for row in rows:
                if row.brand == brand:
                    state[row.purpose] = row.granted
            return state

        table = {b: dict.fromkeys(PURPOSES, False) for b in BRANDS}
        for row in rows:
            if row.brand in table:
                table[row.brand][row.purpose] = row.granted
        return table
