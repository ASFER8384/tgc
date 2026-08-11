"""Outbound mail, behind one interface.

The provider is a detail. What is not a detail is that this is the first part of
the system that can reach a person outside the building, so the guards live here
rather than in whichever provider happens to be configured: sending is off unless
someone turns it on, and a test environment cannot mail a real supplier even when
the address is sitting in the database.

SMTP from a mailbox you already own, rather than an email platform, for one
reason beyond avoiding domain verification: replies come back to that same
mailbox. A sending-only service leaves the acknowledgement half of the loop with
nowhere to arrive, and the acknowledgement half is most of the value.
"""

import asyncio
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Protocol

from sca.config import Settings


class MailError(RuntimeError):
    """Raised when a message cannot be sent, or must not be."""


@dataclass(frozen=True)
class OutboundMessage:
    to: str
    subject: str
    body: str
    to_name: str | None = None


class Mailer(Protocol):
    name: str

    async def deliver(self, message: OutboundMessage) -> dict: ...


class NullMailer:
    """Sends nothing. The default, deliberately.

    A system that mails suppliers the moment it is deployed is a system nobody
    can safely try out.
    """

    name = "none"

    async def deliver(self, message: OutboundMessage) -> dict:
        return {"provider": self.name, "delivered": False, "reason": "mail is not configured"}


class ConsoleMailer:
    """Writes the message to the log instead of the wire.

    Useful precisely because it proves the composition and the addressing without
    involving anyone else's inbox.
    """

    name = "console"

    def __init__(self) -> None:
        self.sent: list[OutboundMessage] = []

    async def deliver(self, message: OutboundMessage) -> dict:
        self.sent.append(message)
        print(f"[mail:console] to={message.to} subject={message.subject}\n{message.body}\n")
        return {"provider": self.name, "delivered": True, "recipient": message.to}


class SmtpMailer:
    """Plain SMTP with STARTTLS, which every mailbox provider speaks.

    Gmail and Microsoft 365 both reject an account password here and want an app
    password against an account with two factor enabled. That failure arrives as a
    bare 535, so it is worth knowing before reading the traceback.
    """

    name = "smtp"

    def __init__(self, settings: Settings) -> None:
        missing = [
            key for key, value in (
                ("SCA_MAIL_SMTP_USER", settings.mail_smtp_user),
                ("SCA_MAIL_SMTP_PASSWORD", settings.mail_smtp_password),
                ("SCA_MAIL_FROM", settings.mail_from),
            ) if not value
        ]
        if missing:
            raise MailError(f"SMTP needs {', '.join(missing)}")
        self.settings = settings

    async def deliver(self, message: OutboundMessage) -> dict:
        mail = EmailMessage()
        mail["From"] = f"{self.settings.mail_from_name} <{self.settings.mail_from}>"
        mail["To"] = f"{message.to_name} <{message.to}>" if message.to_name else message.to
        mail["Subject"] = message.subject
        # Replies belong in the mailbox the inbound side watches, which is not
        # necessarily the one we send from.
        mail["Reply-To"] = self.settings.mail_reply_to or self.settings.mail_from
        mail.set_content(message.body)

        # smtplib is blocking and there is no async SMTP in the standard library.
        # A worker thread keeps one slow supplier's mail server from stalling
        # every other request on the event loop.
        await asyncio.to_thread(self._send, mail)
        return {
            "provider": self.name,
            "delivered": True,
            "recipient": message.to,
            "host": self.settings.mail_smtp_host,
        }

    def _send(self, mail: EmailMessage) -> None:
        settings = self.settings
        # Two conventions, and providers are split between them: 587 negotiates
        # TLS after connecting, 465 is encrypted from the first byte. Choosing on
        # the port rather than asking means most providers need a host and nothing
        # else, and a wrong choice fails as a timeout that explains nothing.
        implicit_tls = settings.mail_smtp_port == 465
        try:
            if implicit_tls:
                with smtplib.SMTP_SSL(
                    settings.mail_smtp_host, settings.mail_smtp_port, timeout=30
                ) as smtp:
                    smtp.login(settings.mail_smtp_user, settings.mail_smtp_password)
                    smtp.send_message(mail)
                return
            with smtplib.SMTP(settings.mail_smtp_host, settings.mail_smtp_port, timeout=30) as smtp:
                if settings.mail_smtp_starttls:
                    smtp.starttls()
                smtp.login(settings.mail_smtp_user, settings.mail_smtp_password)
                smtp.send_message(mail)
        except smtplib.SMTPAuthenticationError as exc:
            raise MailError(
                "SMTP rejected the credentials. Gmail and Microsoft 365 require an "
                "app password on an account with two factor authentication, not the "
                f"account password: {exc}"
            ) from exc
        except (smtplib.SMTPException, OSError) as exc:
            raise MailError(f"SMTP delivery failed: {exc}") from exc


def resolve_recipient(address: str | None, settings: Settings) -> str:
    """Where this message may actually go, which is not always where it is addressed.

    Two guards, both about the same failure: the demo data contains invented
    supplier addresses on domains that belong to somebody. A redirect sends
    everything to one mailbox regardless of the order, and an allowlist refuses
    anything else outright. Without them the first real send in a test environment
    is unsolicited mail to a stranger.
    """
    if not address:
        raise MailError("supplier has no email address on file")
    if settings.mail_redirect_to:
        return settings.mail_redirect_to
    if settings.mail_allowed_domains:
        domain = address.rsplit("@", 1)[-1].lower()
        if domain not in {d.lower() for d in settings.mail_allowed_domains}:
            raise MailError(
                f"{address} is outside SCA_MAIL_ALLOWED_DOMAINS. Set "
                "SCA_MAIL_REDIRECT_TO to route test mail to your own inbox instead"
            )
    return address


def get_mailer(settings: Settings) -> Mailer:
    if settings.mail_provider == "smtp":
        return SmtpMailer(settings)
    if settings.mail_provider == "console":
        return ConsoleMailer()
    return NullMailer()
