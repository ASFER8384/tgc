"""Supplier replies coming in.

One endpoint, whatever the mailbox. Microsoft 365, Google Workspace or plain IMAP
all deliver the same four fields, so the poller that fetches them is a small
adapter rather than a second copy of this logic.
"""

from datetime import datetime

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from sca.api.deps import ActorDep, SessionDep
from sca.config import get_settings
from sca.inbound.parser import parse
from sca.inbound.service import InboundService
from sca.mail import MailError
from sca.mail.inbox import fetch_unread, mark_seen

router = APIRouter(prefix="/inbound", tags=["inbound"])


class EmailIn(BaseModel):
    # The mail server's own id. It is what makes reprocessing a mailbox safe.
    external_id: str
    from_address: str | None = None
    subject: str | None = None
    body: str = ""
    received_at: datetime | None = None


@router.post("/email")
async def inbound_email(body: EmailIn, session: SessionDep, actor: ActorDep) -> dict:
    return await InboundService(session, actor=actor).ingest(
        external_id=body.external_id,
        from_address=body.from_address,
        subject=body.subject,
        body=body.body,
        received_at=body.received_at,
    )


@router.post("/poll")
async def poll_mailbox(session: SessionDep, actor: ActorDep, limit: int = 25) -> dict:
    """Read the mailbox and apply whatever the suppliers have sent.

    Safe to call as often as you like. Messages are keyed by their Message-ID and
    the ingest path already refuses one it has seen, so a double poll cannot
    acknowledge an order twice.
    """
    settings = get_settings()
    try:
        messages = await fetch_unread(settings, limit=limit)
    except MailError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    service = InboundService(session, actor=actor)
    results: list[dict] = []
    taken: list[str] = []
    skipped = 0
    for message in messages:
        # The mailbox belongs to a person as well as to the system. Anything not
        # from a supplier we know is left alone entirely: not ingested, and not
        # marked read, so the owner finds their inbox as they left it.
        if await service.match_supplier(message.from_address) is None:
            skipped += 1
            continue
        outcome = await service.ingest(
            external_id=message.external_id,
            from_address=message.from_address,
            subject=message.subject,
            body=message.body,
            received_at=message.received_at,
        )
        results.append(
            {"from": message.from_address, "subject": message.subject} | outcome
        )
        taken.append(message.uid)

    if taken:
        await mark_seen(settings, taken)
    return {
        "read": len(messages),
        "from_suppliers": len(results),
        "skipped_not_a_supplier": skipped,
        "applied": len([r for r in results if not r.get("duplicate")]),
        "messages": results,
    }


class PreviewIn(BaseModel):
    subject: str | None = None
    body: str = ""


@router.post("/preview")
async def preview(body: PreviewIn, actor: ActorDep) -> dict:
    """Read a message without touching anything.

    Useful in a demo, and more useful in practice: it is how a buyer checks what
    the extractor would do with a supplier's phrasing before trusting it.
    """
    return parse(body.subject, body.body).as_dict()
