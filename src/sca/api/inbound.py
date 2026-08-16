"""Supplier replies coming in.

One endpoint, whatever the mailbox. Microsoft 365, Google Workspace or plain IMAP
all deliver the same four fields, so the poller that fetches them is a small
adapter rather than a second copy of this logic.
"""

import base64
import binascii
from datetime import datetime

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel
from sqlalchemy import select

from sca.api.deps import ActorDep, SessionDep
from sca.config import get_settings
from sca.inbound.parser import parse
from sca.inbound.service import InboundService, IncomingFile
from sca.mail import MailError
from sca.mail.inbox import fetch_unread, mark_seen
from sca.models import Attachment

router = APIRouter(prefix="/inbound", tags=["inbound"])


class FileIn(BaseModel):
    filename: str
    content_type: str = ""
    # Base64 because JSON has no bytes, and this endpoint is the same one a
    # mailbox adapter posts to.
    content_base64: str


class EmailIn(BaseModel):
    # The mail server's own id. It is what makes reprocessing a mailbox safe.
    external_id: str
    from_address: str | None = None
    subject: str | None = None
    body: str = ""
    received_at: datetime | None = None
    attachments: list[FileIn] = []


def _decoded_files(files: list[FileIn]) -> list[IncomingFile]:
    out: list[IncomingFile] = []
    for item in files:
        try:
            content = base64.b64decode(item.content_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"{item.filename}: content_base64 is not valid base64",
            ) from exc
        out.append(
            IncomingFile(
                filename=item.filename, content_type=item.content_type, content=content
            )
        )
    return out


@router.post("/email")
async def inbound_email(body: EmailIn, session: SessionDep, actor: ActorDep) -> dict:
    return await InboundService(session, actor=actor).ingest(
        external_id=body.external_id,
        from_address=body.from_address,
        subject=body.subject,
        body=body.body,
        received_at=body.received_at,
        attachments=_decoded_files(body.attachments),
    )


@router.get("/attachments/{attachment_id}")
async def download_attachment(
    attachment_id: str, session: SessionDep, actor: ActorDep
) -> Response:
    """The file exactly as it crossed, in whichever direction it crossed.

    Served as an octet stream and as a download rather than inline: half of these
    are files from outside the building, and a browser rendering one in a page
    that holds an API key is a decision nobody made deliberately.
    """
    row = await session.scalar(select(Attachment).where(Attachment.id == attachment_id))
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown attachment")
    safe = row.filename.replace('"', "").replace("\\", "")
    return Response(
        content=row.content,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{safe}"'},
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
            attachments=[
                IncomingFile(
                    filename=f.filename, content_type=f.content_type, content=f.content
                )
                for f in message.attachments
            ],
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
