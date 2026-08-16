"""Outbound WhatsApp, behind one interface.

The same shape as `sca.mail`, and for the same reason: this is a second way to
reach someone outside the building, so the guards live here rather than in
whichever caller happens to be sending. Off unless somebody turns it on, and a
test environment cannot message a real supplier even with the number on file.

What differs from mail, and governs the design:

A business cannot start a WhatsApp conversation in its own words. Meta requires
an approved template for any message the business sends first — there is no
exception for operational mail, so a purchase order to a supplier who has not
written to us today is a template, not the letter. Once they reply, a
twenty-four hour window opens in which free text is allowed.

So there are three ways to send and they are not interchangeable: `send_template`
opens a conversation, `send_text` and `send_media` continue one. Which is allowed
depends on when the supplier last wrote, which is a fact about the record rather
than about this module — so the window is checked by the caller, before any of
them is reached.

Files go both ways and neither direction is one call. Sending is an upload to
Meta followed by a send quoting the id it gave back; receiving is a webhook
carrying an id, a lookup that turns it into a short-lived URL, and a download
with the same token. That is why `upload_media` and `download_media` are here
rather than in the caller: the token and the version belong to this module, and
a URL Meta hands out expires within minutes.
"""

import json
import mimetypes
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Protocol

from sca.config import Settings

GRAPH = "https://graph.facebook.com"

# Meta's own ceiling is 100MB for documents and 5MB for images. This is lower on
# purpose and matched to the mail side: the bytes are kept in the database, so a
# file too large to store is one that must be refused before it is sent, not
# after it has arrived at the supplier and failed to be filed here.
MAX_MEDIA_BYTES = 10_000_000


class WhatsAppError(RuntimeError):
    """Raised when a message cannot be sent, or must not be."""


@dataclass(frozen=True)
class TemplateMessage:
    """A template send, addressed and filled in.

    ``variables`` are positional because the template is: Meta numbers them
    {{1}}, {{2}} and matches by order alone, so a named map here would be a
    second ordering to keep in step with the first.
    """

    to: str
    template: str
    language: str
    variables: tuple[str, ...] = ()


@dataclass(frozen=True)
class TextMessage:
    """Free text, which is only legal inside the customer service window.

    Meta allows a business to write in its own words for twenty-four hours after
    the other side writes first. Outside that, a send is refused at their edge —
    so the window is checked here rather than discovered as a failed order.
    """

    to: str
    text: str


@dataclass(frozen=True)
class MediaMessage:
    """A file on its way to a supplier, with the words that go with it.

    ``kind`` is WhatsApp's, not ours: an image arrives in the chat as a picture
    and a document as a named row you tap to download. A packing list photographed
    on a phone belongs in the first; an invoice PDF in the second, because a
    document keeps its filename and an image does not.
    """

    to: str
    filename: str
    content_type: str
    content: bytes
    caption: str = ""

    @property
    def kind(self) -> str:
        return "image" if self.content_type.lower().startswith("image/") else "document"


@dataclass(frozen=True)
class Media:
    """A file that arrived, once it has actually been fetched."""

    media_id: str
    filename: str
    content_type: str
    content: bytes


class Sender(Protocol):
    name: str

    async def send_template(self, message: TemplateMessage) -> dict: ...

    async def send_text(self, message: TextMessage) -> dict: ...

    async def send_media(self, message: MediaMessage) -> dict: ...


class NullSender:
    """Sends nothing. The default, deliberately."""

    name = "none"

    async def send_template(self, message: TemplateMessage) -> dict:
        return self._off()

    async def send_text(self, message: TextMessage) -> dict:
        return self._off()

    async def send_media(self, message: MediaMessage) -> dict:
        return self._off()

    def _off(self) -> dict:
        return {
            "provider": self.name,
            "delivered": False,
            "reason": "whatsapp is not configured",
        }


@dataclass
class ConsoleSender:
    """Writes the message to the log instead of the wire.

    Proves the addressing and the variable order without involving anyone's
    handset, which is what the tests and the demo run against.
    """

    name: str = "console"
    sent: list[TemplateMessage | TextMessage | MediaMessage] = field(default_factory=list)

    async def send_template(self, message: TemplateMessage) -> dict:
        self.sent.append(message)
        print(
            f"[whatsapp:console] to={message.to} template={message.template}"
            f" vars={list(message.variables)}"
        )
        return {"provider": self.name, "delivered": True, "recipient": message.to}

    async def send_text(self, message: TextMessage) -> dict:
        self.sent.append(message)
        print(f"[whatsapp:console] to={message.to} text={message.text[:60]!r}")
        return {"provider": self.name, "delivered": True, "recipient": message.to}

    async def send_media(self, message: MediaMessage) -> dict:
        self.sent.append(message)
        print(
            f"[whatsapp:console] to={message.to} {message.kind}={message.filename}"
            f" ({len(message.content)} bytes)"
        )
        return {"provider": self.name, "delivered": True, "recipient": message.to}


class CloudSender:
    """Meta's WhatsApp Cloud API.

    Errors are read out of Meta's envelope rather than the HTTP status. A bad
    template name and an unreachable number both arrive as a 400 with the
    difference only in the body, and "400 Bad Request" in a log is not something
    anybody can act on.
    """

    name = "cloud"

    def __init__(self, settings: Settings) -> None:
        missing = [
            key
            for key, value in (
                ("SCA_WA_ACCESS_TOKEN", settings.wa_access_token),
                ("SCA_WA_PHONE_NUMBER_ID", settings.wa_phone_number_id),
            )
            if not value
        ]
        if missing:
            raise WhatsAppError(f"WhatsApp Cloud needs {', '.join(missing)}")
        self.settings = settings

    async def send_template(self, message: TemplateMessage) -> dict:
        import asyncio

        payload = {
            "messaging_product": "whatsapp",
            "to": message.to,
            "type": "template",
            "template": {
                "name": message.template,
                "language": {"code": message.language},
            },
        }
        if message.variables:
            payload["template"]["components"] = [
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": str(v)} for v in message.variables
                    ],
                }
            ]

        # urllib on a worker thread rather than an async client, to keep the
        # dependency list as it is. One supplier's slow send must not stall the
        # event loop for every other request.
        return await asyncio.to_thread(self._post, payload)

    async def send_text(self, message: TextMessage) -> dict:
        import asyncio

        return await asyncio.to_thread(self._post, {
            "messaging_product": "whatsapp",
            "to": message.to,
            "type": "text",
            # Off, deliberately. Link previews are fetched by Meta from whatever
            # the text happens to contain, and a purchase order is not a place
            # for a preview card nobody chose.
            "text": {"preview_url": False, "body": message.text},
        })

    async def send_media(self, message: MediaMessage) -> dict:
        import asyncio

        if len(message.content) > MAX_MEDIA_BYTES:
            raise WhatsAppError(
                f"{message.filename} is {len(message.content) / 1e6:.1f}MB, over the "
                f"{MAX_MEDIA_BYTES / 1e6:.0f}MB limit"
            )
        return await asyncio.to_thread(self._send_media, message)

    def _send_media(self, message: MediaMessage) -> dict:
        # Two calls, and they must stay together. The id an upload returns is
        # good for thirty days but means nothing to anyone else, so uploading
        # without sending leaves a file in Meta's store that no record here
        # points at.
        media_id = self._upload(message.filename, message.content_type, message.content)
        body: dict = {"id": media_id}
        if message.caption:
            body["caption"] = message.caption
        if message.kind == "document":
            # An image carries no filename in WhatsApp; a document does, and it
            # is what the supplier sees in the chat before opening it.
            body["filename"] = message.filename
        return self._post({
            "messaging_product": "whatsapp",
            "to": message.to,
            "type": message.kind,
            message.kind: body,
        }) | {"media_id": media_id}

    def _upload(self, filename: str, content_type: str, content: bytes) -> str:
        boundary = f"----sca{uuid.uuid4().hex}"
        parts: list[bytes] = []

        def field_part(name: str, value: str) -> None:
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n".encode()
            )

        field_part("messaging_product", "whatsapp")
        field_part("type", content_type)
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="file";'
            f' filename="{filename}"\r\nContent-Type: {content_type}\r\n\r\n'.encode()
        )
        parts.append(content)
        parts.append(f"\r\n--{boundary}--\r\n".encode())

        settings = self.settings
        url = f"{GRAPH}/{settings.wa_api_version}/{settings.wa_phone_number_id}/media"
        body = self._call(
            urllib.request.Request(
                url, data=b"".join(parts), method="POST",
                headers={
                    "Authorization": f"Bearer {settings.wa_access_token}",
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                },
            )
        )
        media_id = json.loads(body).get("id")
        if not media_id:
            raise WhatsAppError(f"{filename} uploaded but Meta returned no media id")
        return media_id

    def _post(self, payload: dict) -> dict:
        settings = self.settings
        url = (
            f"{GRAPH}/{settings.wa_api_version}/"
            f"{settings.wa_phone_number_id}/messages"
        )
        body = json.loads(self._call(
            urllib.request.Request(
                url,
                data=json.dumps(payload).encode(),
                headers={
                    "Authorization": f"Bearer {settings.wa_access_token}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
        ))

        messages = body.get("messages") or [{}]
        return {
            "provider": self.name,
            "delivered": True,
            # Meta echoes the number it will actually deliver to, which can
            # differ from what was sent when a country writes numbers two ways.
            "recipient": (body.get("contacts") or [{}])[0].get("wa_id"),
            "message_id": messages[0].get("id"),
        }

    @staticmethod
    def _call(request: urllib.request.Request, timeout: int = 60) -> bytes:
        """One request to Meta, with their error envelope read out of the body.

        A bad template name and an unreachable number both arrive as a 400 and
        differ only in the body, so "400 Bad Request" in a log is not something
        anybody can act on. Shared by the message calls and the media ones
        because Meta answers all of them the same way.
        """
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read()).get("error", {})
            except Exception:  # noqa: BLE001 - the body is not always JSON
                detail = {}
            raise WhatsAppError(
                detail.get("error_user_msg")
                or detail.get("message")
                or f"WhatsApp call failed with HTTP {exc.code}"
            ) from exc
        except OSError as exc:
            raise WhatsAppError(f"WhatsApp call failed: {exc}") from exc


async def download_media(
    media_id: str, settings: Settings, *, filename: str | None = None
) -> Media:
    """Fetch a file a supplier sent, given the id the webhook carried.

    Two calls again, and the order matters: the first turns the id into a URL,
    the second downloads it. The URL is short-lived and bound to our token, so
    it cannot be stored and read later — the bytes have to be pulled now, while
    the webhook is being handled, or they are gone.
    """
    import asyncio

    if not settings.wa_access_token:
        raise WhatsAppError("WhatsApp media needs SCA_WA_ACCESS_TOKEN")

    def _fetch() -> Media:
        auth = {
            "Authorization": f"Bearer {settings.wa_access_token}",
            # Meta serves media from a CDN that refuses urllib's default agent.
            "User-Agent": "sca/1.0",
        }
        described = json.loads(CloudSender._call(urllib.request.Request(
            f"{GRAPH}/{settings.wa_api_version}/{media_id}", headers=auth
        )))
        url = described.get("url")
        if not url:
            raise WhatsAppError(f"media {media_id} has no download url")
        size = int(described.get("file_size") or 0)
        if size > MAX_MEDIA_BYTES:
            raise WhatsAppError(
                f"the file is {size / 1e6:.1f}MB, over the "
                f"{MAX_MEDIA_BYTES / 1e6:.0f}MB this system stores"
            )
        content = CloudSender._call(urllib.request.Request(url, headers=auth))
        mime = (described.get("mime_type") or "application/octet-stream").split(";")[0]
        return Media(
            media_id=media_id,
            filename=filename or _named(media_id, mime),
            content_type=mime,
            content=content,
        )

    return await asyncio.to_thread(_fetch)


def _named(media_id: str, mime: str) -> str:
    """A filename for something that arrived without one.

    Photographs sent from a phone have no name at all, and "attachment" on every
    one of them makes a folder nobody can read. The id is at least the thing the
    row can be traced back to.
    """
    return f"whatsapp-{media_id[:12]}{mimetypes.guess_extension(mime) or ''}"


def resolve_recipient(number: str | None, settings: Settings) -> str:
    """Where this message may actually go, which is not always where it is addressed.

    The same two guards as mail, and the same reasoning: a number on a demo
    supplier belongs to somebody. A redirect routes everything to one handset; an
    allowlist refuses anything else outright. Without them the first real send in
    a test environment is an unsolicited WhatsApp to a stranger — which, unlike
    an email, arrives with a notification on their phone.
    """
    if not number:
        raise WhatsAppError("supplier has no phone number on file")
    digits = "".join(ch for ch in number if ch.isdigit())
    if not digits:
        raise WhatsAppError(f"{number!r} has no digits in it")
    if settings.wa_redirect_to:
        return "".join(ch for ch in settings.wa_redirect_to if ch.isdigit())
    if settings.wa_allowed_numbers:
        allowed = {
            "".join(ch for ch in n if ch.isdigit()) for n in settings.wa_allowed_numbers
        }
        if digits not in allowed:
            raise WhatsAppError(
                f"{digits} is outside SCA_WA_ALLOWED_NUMBERS. Set SCA_WA_REDIRECT_TO "
                "to route test messages to your own handset instead"
            )
    return digits


def get_sender(settings: Settings) -> Sender:
    if settings.wa_provider == "cloud":
        return CloudSender(settings)
    if settings.wa_provider == "console":
        return ConsoleSender()
    return NullSender()
