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

So there are two ways to send and they are not interchangeable: `send_template`
opens a conversation, `send_text` continues one. Which is allowed depends on
when the supplier last wrote, which is a fact about the record rather than about
this module — so the window is checked by the caller, before either is reached.
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Protocol

from sca.config import Settings

GRAPH = "https://graph.facebook.com"


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


class Sender(Protocol):
    name: str

    async def send_template(self, message: TemplateMessage) -> dict: ...

    async def send_text(self, message: TextMessage) -> dict: ...


class NullSender:
    """Sends nothing. The default, deliberately."""

    name = "none"

    async def send_template(self, message: TemplateMessage) -> dict:
        return self._off()

    async def send_text(self, message: TextMessage) -> dict:
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
    sent: list[TemplateMessage | TextMessage] = field(default_factory=list)

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

    def _post(self, payload: dict) -> dict:
        settings = self.settings
        url = (
            f"{GRAPH}/{settings.wa_api_version}/"
            f"{settings.wa_phone_number_id}/messages"
        )
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {settings.wa_access_token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                detail = json.load(exc).get("error", {})
            except Exception:  # noqa: BLE001 - the body is not always JSON
                detail = {}
            raise WhatsAppError(
                detail.get("error_user_msg")
                or detail.get("message")
                or f"WhatsApp send failed with HTTP {exc.code}"
            ) from exc
        except OSError as exc:
            raise WhatsAppError(f"WhatsApp send failed: {exc}") from exc

        messages = body.get("messages") or [{}]
        return {
            "provider": self.name,
            "delivered": True,
            # Meta echoes the number it will actually deliver to, which can
            # differ from what was sent when a country writes numbers two ways.
            "recipient": (body.get("contacts") or [{}])[0].get("wa_id"),
            "message_id": messages[0].get("id"),
        }


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
