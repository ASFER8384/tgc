from datetime import UTC, datetime

from sqlalchemy import ScalarSelect, select
from sqlalchemy.ext.asyncio import AsyncSession

from cdp.models import PURPOSES, AuditLog, ConsentEvent, Person


class ConsentError(ValueError):
    pass


def latest_consent_subquery(purpose: str, person_id_column) -> ScalarSelect[bool]:
    """The consent gate, as a correlated scalar subquery.

    Every audience query composes this instead of trusting a denormalised flag,
    so consent state can never be stale by one sync cycle. No row at all yields
    NULL, which fails the ``is true`` test — silence is not agreement.
    """
    return (
        select(ConsentEvent.granted)
        .where(ConsentEvent.person_id == person_id_column, ConsentEvent.purpose == purpose)
        .order_by(
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
        source: str,
        evidence: str | None = None,
        occurred_at: datetime | None = None,
    ) -> ConsentEvent:
        if purpose not in PURPOSES:
            raise ConsentError(f"unknown consent purpose: {purpose}")
        if await self.session.get(Person, person_id) is None:
            raise ConsentError(f"unknown person: {person_id}")

        row = ConsentEvent(
            person_id=person_id,
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
                meta={"purpose": purpose, "source": source},
            )
        )
        await self.session.flush()
        return row

    async def current(self, person_id: str) -> dict[str, bool]:
        """Latest state per purpose. Purposes with no record are reported as
        False rather than omitted, so a caller cannot mistake absence for assent."""
        rows = (
            await self.session.scalars(
                select(ConsentEvent)
                .where(ConsentEvent.person_id == person_id)
                .order_by(ConsentEvent.occurred_at, ConsentEvent.created_at, ConsentEvent.id)
            )
        ).all()
        state = dict.fromkeys(PURPOSES, False)
        for row in rows:
            state[row.purpose] = row.granted
        return state
