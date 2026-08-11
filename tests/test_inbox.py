"""Turning a real mail message into the payload the ingest endpoint accepts.

No socket is opened here. What matters is that a message written by Outlook, by a
phone, or by a supplier writing Arabic in the subject line survives the trip into
the parser intact, because everything downstream is only as good as this.
"""

from datetime import UTC, datetime
from email.message import EmailMessage

from sca.mail.inbox import to_inbound


def _raw(**parts) -> bytes:
    mail = EmailMessage()
    mail["Message-ID"] = parts.get("message_id", "<abc123@mail.gmail.com>")
    mail["From"] = parts.get("sender", "Wang Li <orders@gzsilkmill.cn>")
    mail["Subject"] = parts.get("subject", "Re: PO-5002")
    mail["Date"] = parts.get("date", "Mon, 10 Aug 2026 14:05:00 +0800")
    mail.set_content(parts.get("body", "We confirm PO-5002."))
    if parts.get("html"):
        mail.add_alternative(parts["html"], subtype="html")
    return mail.as_bytes()


def test_the_message_id_becomes_the_external_id():
    """Which is what makes polling repeatable: ingest already refuses a repeat."""
    found = to_inbound(_raw(), fallback_id="1")
    assert found.external_id == "<abc123@mail.gmail.com>"


def test_the_display_name_is_stripped_off_the_address():
    found = to_inbound(_raw(), fallback_id="1")
    assert found.from_address == "orders@gzsilkmill.cn"


def test_a_bare_address_survives_unchanged():
    found = to_inbound(_raw(sender="siparis@istpack.com.tr"), fallback_id="1")
    assert found.from_address == "siparis@istpack.com.tr"


def test_an_encoded_subject_is_decoded():
    """Suppliers write Arabic and Chinese subjects, which arrive RFC 2047 encoded."""
    found = to_inbound(_raw(subject="=?utf-8?B?2LTZg9ix2Kc=?="), fallback_id="1")
    assert found.subject == "شكرا"


def test_the_plain_text_part_is_preferred_over_the_html_twin():
    found = to_inbound(
        _raw(body="We confirm PO-5002.", html="<p>We <b>confirm</b> PO-5002.</p>"),
        fallback_id="1",
    )
    assert "We confirm PO-5002." in found.body
    assert "<b>" not in found.body


def test_the_senders_own_timestamp_is_kept():
    """Not the time we happened to poll: a reply sent overnight was sent overnight,
    and the acknowledgement clock is measured from when they wrote it."""
    found = to_inbound(_raw(), fallback_id="1")
    assert found.received_at == datetime(2026, 8, 10, 6, 5, tzinfo=UTC)


def test_a_message_with_no_id_still_gets_one():
    mail = EmailMessage()
    mail["From"] = "orders@gzsilkmill.cn"
    mail.set_content("We confirm.")
    found = to_inbound(mail.as_bytes(), fallback_id="42")
    assert found.external_id == "imap-42"


def test_an_unparseable_date_falls_back_to_now_rather_than_failing():
    found = to_inbound(_raw(date="not a date"), fallback_id="1")
    assert found.received_at.tzinfo is not None


def test_the_payload_matches_what_the_endpoint_expects():
    payload = to_inbound(_raw(), fallback_id="1").as_payload()
    assert set(payload) == {
        "external_id", "from_address", "subject", "body", "received_at",
    }
