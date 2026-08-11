from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from cdp.models import AuditLog, Segment, SegmentMember
from cdp.segments.compiler import SegmentDefinitionError, compile_segment


class SegmentService:
    def __init__(self, session: AsyncSession, *, actor: str = "system"):
        self.session = session
        self.actor = actor

    async def upsert(
        self,
        key: str,
        name: str,
        definition: dict,
        *,
        required_consent: str | None = None,
        description: str | None = None,
    ) -> Segment:
        # Compile before storing. A definition that cannot compile is rejected at
        # authoring time, when someone is there to fix it, rather than at 2am when
        # a campaign job runs.
        compile_segment(definition, required_consent)

        segment = await self.session.scalar(select(Segment).where(Segment.key == key))
        if segment is None:
            segment = Segment(key=key, name=name, definition=definition)
            self.session.add(segment)
        segment.name = name
        segment.definition = definition
        segment.required_consent = required_consent
        segment.description = description

        self.session.add(
            AuditLog(
                actor=self.actor,
                action="segment.upsert",
                entity="segment",
                entity_id=key,
                meta={"required_consent": required_consent},
            )
        )
        await self.session.flush()
        return segment

    async def get(self, key: str) -> Segment | None:
        return await self.session.scalar(select(Segment).where(Segment.key == key))

    async def member_ids(self, segment: Segment) -> list[str]:
        query = compile_segment(segment.definition, segment.required_consent)
        return list(await self.session.scalars(query))

    async def evaluate(self, key: str) -> list[str]:
        """Re-run the definition and materialise membership.

        The stored member list is a cache for the console, never the source of
        truth — activation re-evaluates, so a consent revoked one minute ago is
        honoured on the next send without waiting for a refresh job.
        """
        segment = await self.get(key)
        if segment is None:
            raise SegmentDefinitionError(f"unknown segment: {key}")

        ids = await self.member_ids(segment)
        now = datetime.now(UTC)
        await self.session.execute(
            delete(SegmentMember).where(SegmentMember.segment_id == segment.id)
        )
        for person_id in ids:
            self.session.add(
                SegmentMember(segment_id=segment.id, person_id=person_id, evaluated_at=now)
            )
        await self.session.flush()
        return ids
