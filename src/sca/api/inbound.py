"""Supplier replies coming in.

One endpoint, whatever the mailbox. Microsoft 365, Google Workspace or plain IMAP
all deliver the same four fields, so the poller that fetches them is a small
adapter rather than a second copy of this logic.
"""

from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel

from sca.api.deps import ActorDep, SessionDep
from sca.inbound.parser import parse
from sca.inbound.service import InboundService

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
