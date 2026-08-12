"""Reading the supplier's reply out of a real mailbox.

This is the half that makes the system self driving. Sending is the easy
direction; the value is in what comes back, and until now a reply reached the
system only because a person pasted it into a form.

IMAP rather than a provider webhook on purpose: it works against any mailbox the
business already has, needs no domain, no public URL and no vendor. The cost is
polling rather than push, which for a supplier reply is a difference nobody can
feel.

Nothing here interprets the message. Classification stays in the parser and the
decision to act stays in the inbound service; this module only turns bytes on a
socket into the same payload the console posts by hand.
"""

import asyncio
import base64
import email
import imaplib
from dataclasses import dataclass
from datetime import UTC, datetime
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parsedate_to_datetime

from sca.config import Settings
from sca.mail.base import MailError


@dataclass(frozen=True)
class InboundFile:
    """One attachment, as it arrived."""

    filename: str
    content_type: str
    content: bytes

    def as_payload(self) -> dict:
        return {
            "filename": self.filename,
            "content_type": self.content_type,
            "content_base64": base64.b64encode(self.content).decode("ascii"),
        }


@dataclass(frozen=True)
class InboundEmail:
    external_id: str
    from_address: str
    subject: str
    body: str
    received_at: datetime
    attachments: tuple[InboundFile, ...] = ()
    # The server side handle, kept so the caller can mark this message read only
    # once it has decided the message was for us.
    uid: str = ""

    def as_payload(self) -> dict:
        return {
            "external_id": self.external_id,
            "from_address": self.from_address,
            "subject": self.subject,
            "body": self.body,
            "received_at": self.received_at.isoformat(),
            "attachments": [a.as_payload() for a in self.attachments],
        }


def _decoded(value: str | None) -> str:
    """Header text as a human wrote it.

    Subjects arrive RFC 2047 encoded whenever they contain anything outside
    ASCII, which for these suppliers is routine rather than exotic.
    """
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except (UnicodeDecodeError, LookupError, ValueError):
        return value


def _address(value: str | None) -> str:
    """Just the address out of `Name <a@b.com>`."""
    raw = _decoded(value)
    if "<" in raw and ">" in raw:
        return raw.split("<", 1)[1].split(">", 1)[0].strip().lower()
    return raw.strip().lower()


def _body(message: Message) -> str:
    """The plain text part, falling back to whatever else is there.

    Suppliers reply from Outlook and from phones, so a message is usually
    multipart with an HTML twin. The parser wants prose, and the text part is
    prose; the HTML part is the same words wrapped in markup that would only add
    noise to the keyword rules.
    """
    if not message.is_multipart():
        return _payload_text(message)

    for part in message.walk():
        if part.get_content_type() == "text/plain" and "attachment" not in str(
            part.get("Content-Disposition") or ""
        ):
            return _payload_text(part)
    for part in message.walk():
        if part.get_content_type() == "text/html":
            return _strip_tags(_payload_text(part))
    return ""


def _payload_text(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        return str(part.get_payload() or "")
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _attachments(message: Message, *, max_bytes: int) -> tuple[InboundFile, ...]:
    """The files, separated from the prose.

    A supplier sends the invoice as a PDF and writes "please find attached" in
    the body. Reading only the body is reading the sentence about the document
    instead of the document.

    Inline images are skipped: a signature logo repeated on every reply is not
    evidence of anything, and storing it on each message would cost more than
    every real attachment put together. Anything over the limit is skipped
    loudly rather than truncated, because half a PDF is worse than none.
    """
    if not message.is_multipart():
        return ()

    found: list[InboundFile] = []
    for part in message.walk():
        if part.get_content_maintype() == "multipart":
            continue
        disposition = str(part.get("Content-Disposition") or "")
        filename = _decoded(part.get_filename())
        # A part is an attachment if it says so, or if it has a filename and is
        # not the body. Outlook omits the disposition often enough that
        # requiring it loses real documents.
        if "attachment" not in disposition.lower() and not filename:
            continue
        if "inline" in disposition.lower() and part.get_content_maintype() == "image":
            continue
        payload = part.get_payload(decode=True)
        if not payload or len(payload) > max_bytes:
            continue
        found.append(
            InboundFile(
                filename=filename or "attachment",
                content_type=part.get_content_type(),
                content=payload,
            )
        )
    return tuple(found)


def _strip_tags(html: str) -> str:
    out, inside = [], False
    for char in html:
        if char == "<":
            inside = True
        elif char == ">":
            inside = False
        elif not inside:
            out.append(char)
    return " ".join("".join(out).split())


def to_inbound(
    raw: bytes, *, fallback_id: str, max_attachment_bytes: int = 10_000_000
) -> InboundEmail:
    """One fetched message, as the payload the ingest endpoint already accepts.

    The Message-ID becomes the external id, which is what makes polling safe to
    repeat: the inbound service already refuses a message it has seen, so a
    double poll cannot acknowledge an order twice.
    """
    message = email.message_from_bytes(raw)
    message_id = (message.get("Message-ID") or "").strip() or f"imap-{fallback_id}"
    try:
        received = parsedate_to_datetime(message.get("Date"))
    except (TypeError, ValueError):
        received = None
    if received is None:
        received = datetime.now(UTC)
    if received.tzinfo is None:
        received = received.replace(tzinfo=UTC)

    return InboundEmail(
        external_id=message_id,
        from_address=_address(message.get("From")),
        subject=_decoded(message.get("Subject")),
        body=_body(message),
        received_at=received,
        attachments=_attachments(message, max_bytes=max_attachment_bytes),
        uid=fallback_id,
    )


def _fetch_blocking(settings: Settings, limit: int) -> list[InboundEmail]:
    host = settings.mail_imap_host
    user = settings.mail_imap_user or settings.mail_smtp_user
    password = settings.mail_imap_password or settings.mail_smtp_password
    if not user or not password:
        raise MailError(
            "reading the mailbox needs SCA_MAIL_IMAP_USER and SCA_MAIL_IMAP_PASSWORD, "
            "or the SMTP credentials to fall back on"
        )

    try:
        with imaplib.IMAP4_SSL(host, settings.mail_imap_port, timeout=45) as imap:
            imap.login(user, password)
            imap.select(settings.mail_imap_folder)
            status, data = imap.search(None, settings.mail_imap_search)
            if status != "OK":
                raise MailError(f"mailbox search failed: {status}")

            ids = data[0].split()
            # The most recent, not the first. A mailbox with more unread mail than
            # the limit would otherwise never reach today's replies: the window
            # would sit permanently over the oldest backlog, which is exactly the
            # mail nobody is waiting on. Within the window the order is preserved,
            # so a supplier who wrote twice is applied in the order they wrote.
            ids = ids[-limit:] if limit else ids

            found: list[InboundEmail] = []
            for message_id in ids:
                status, payload = imap.fetch(message_id, "(RFC822)")
                if status != "OK" or not payload or not isinstance(payload[0], tuple):
                    continue
                found.append(
                    to_inbound(
                        payload[0][1],
                        fallback_id=message_id.decode(),
                        max_attachment_bytes=settings.mail_max_attachment_bytes,
                    )
                )
            # Deliberately not marked read here. The mailbox that receives supplier
            # replies also receives everything else a person gets, and reading a
            # message is not the same as it being ours. The caller marks only what
            # it recognised, so a personal mail the poller looked at and skipped is
            # left exactly as the owner left it.
            return found
    except imaplib.IMAP4.error as exc:
        raise MailError(
            f"mailbox refused the connection or the credentials: {exc}. Gmail needs "
            "an app password here, the same one SMTP uses"
        ) from exc
    except OSError as exc:
        raise MailError(f"could not reach {host}: {exc}") from exc


def _mark_seen_blocking(settings: Settings, uids: list[str], seen: bool) -> int:
    if not uids:
        return 0
    user = settings.mail_imap_user or settings.mail_smtp_user
    password = settings.mail_imap_password or settings.mail_smtp_password
    try:
        with imaplib.IMAP4_SSL(
            settings.mail_imap_host, settings.mail_imap_port, timeout=45
        ) as imap:
            imap.login(user, password)
            imap.select(settings.mail_imap_folder)
            for uid in uids:
                imap.store(uid, "+FLAGS" if seen else "-FLAGS", "\\Seen")
            return len(uids)
    except (imaplib.IMAP4.error, OSError) as exc:
        raise MailError(f"could not update message flags: {exc}") from exc


async def mark_seen(settings: Settings, uids: list[str], *, seen: bool = True) -> int:
    """Flag the messages we actually took, and only those."""
    return await asyncio.to_thread(_mark_seen_blocking, settings, uids, seen)


async def fetch_unread(settings: Settings, *, limit: int = 25) -> list[InboundEmail]:
    """Unread messages, marked read as they are taken.

    imaplib blocks, and a mail server on the other side of the world blocks for a
    while, so it runs off the event loop.
    """
    return await asyncio.to_thread(_fetch_blocking, settings, limit)
