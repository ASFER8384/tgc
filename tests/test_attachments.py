"""The file the supplier actually sent.

Until now the system read the sentence about the document instead of the
document: an email saying "invoice attached" produced a Document row with a
filename this code invented, pointing at bytes nobody had stored. These tests
pin the two halves of the fix — the attachment survives the trip out of the
mailbox, and a document exists only where a file does.
"""

import base64
from email.message import EmailMessage

import pytest

from sca.mail.inbox import to_inbound

PDF = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n"


def _raw(*, body="Please find the invoice attached for PO-5002.", files=(), html=None) -> bytes:
    mail = EmailMessage()
    mail["Message-ID"] = "<inv-1@mail.gmail.com>"
    mail["From"] = "Wang Li <orders@gzsilkmill.cn>"
    mail["Subject"] = "Invoice PO-5002"
    mail["Date"] = "Mon, 10 Aug 2026 14:05:00 +0800"
    mail.set_content(body)
    if html:
        mail.add_alternative(html, subtype="html")
    for filename, maintype, subtype, content in files:
        mail.add_attachment(content, maintype=maintype, subtype=subtype, filename=filename)
    return mail.as_bytes()


# ------------------------------------------------------------------ the mailbox
def test_the_attachment_comes_out_of_the_message():
    found = to_inbound(
        _raw(files=[("invoice-8841.pdf", "application", "pdf", PDF)]), fallback_id="1"
    )
    assert [a.filename for a in found.attachments] == ["invoice-8841.pdf"]
    assert found.attachments[0].content == PDF
    assert found.attachments[0].content_type == "application/pdf"


def test_the_body_still_reads_as_prose_when_a_file_rides_along():
    """The parser wants the sentence, not the PDF, and both have to survive."""
    found = to_inbound(
        _raw(files=[("invoice-8841.pdf", "application", "pdf", PDF)]), fallback_id="1"
    )
    assert "invoice attached" in found.body
    assert "%PDF" not in found.body


def test_a_message_with_no_files_has_no_attachments():
    assert to_inbound(_raw(), fallback_id="1").attachments == ()


def test_a_file_over_the_limit_is_skipped_rather_than_truncated():
    """Half a PDF is worse than none: it would extract, and extract wrongly."""
    found = to_inbound(
        _raw(files=[("catalogue.pdf", "application", "pdf", PDF * 500)]),
        fallback_id="1",
        max_attachment_bytes=1000,
    )
    assert found.attachments == ()


def test_the_html_twin_is_not_mistaken_for_an_attachment():
    found = to_inbound(
        _raw(html="<p>Please find the invoice attached.</p>"), fallback_id="1"
    )
    assert found.attachments == ()


# ------------------------------------------------------------------- the ingest
def _payload(files=(), *, body, subject="Invoice"):
    return {
        "external_id": "<inv-1@mail.gmail.com>",
        "from_address": "orders@gzsilkmill.cn",
        "subject": subject,
        "body": body,
        "attachments": [
            {
                "filename": name,
                "content_type": content_type,
                "content_base64": base64.b64encode(content).decode(),
            }
            for name, content_type, content in files
        ],
    }


async def _order(client, supplier_payload) -> str:
    """A supplier, an approved order, sent — the state a reply arrives into."""
    supplier = (await client.post("/suppliers", json=supplier_payload)).json()
    created = await client.post(
        "/purchase-orders",
        json={
            "supplier_id": supplier["id"],
            "lines": [{"sku": "SILK-001", "quantity": 100, "unit_price": "12.00"}],
        },
    )
    order = created.json()
    await client.post(f"/purchase-orders/{order['number']}/approve", json={"approver": "buyer"})
    await client.post(f"/purchase-orders/{order['number']}/send")
    return order["number"]


@pytest.mark.asyncio
async def test_the_file_is_stored_and_can_be_downloaded_again(client, supplier_payload):
    number = await _order(client, supplier_payload)
    body = f"Please find the invoice attached for {number}."

    result = await client.post(
        "/inbound/email", json=_payload([("invoice.pdf", "application/pdf", PDF)], body=body)
    )
    assert result.status_code == 200
    stored = result.json()["attachments"]
    assert len(stored) == 1
    assert stored[0]["bytes"] == len(PDF)

    got = await client.get(f"/inbound/attachments/{stored[0]['id']}")
    assert got.status_code == 200
    # Byte for byte. Anything else and re-extraction later answers a different
    # question from the one the supplier sent.
    assert got.content == PDF


@pytest.mark.asyncio
async def test_the_filed_document_names_the_real_file(client, supplier_payload):
    number = await _order(client, supplier_payload)
    await client.post(
        "/inbound/email",
        json=_payload(
            [("GZ-8841.pdf", "application/pdf", PDF)],
            body=f"Please find the invoice attached for {number}.",
        ),
    )
    detail = (await client.get(f"/purchase-orders/{number}")).json()
    assert len(detail["documents"]) == 1
    document = detail["documents"][0]
    assert document["filename"] == "GZ-8841.pdf"
    assert document["attachment_id"] is not None
    assert document["byte_size"] == len(PDF)


@pytest.mark.asyncio
async def test_an_invoice_with_nothing_attached_files_no_document(client, supplier_payload):
    """The bug this exists to prevent: a filename in the order history that
    nobody could open, invented from the word "invoice" in a message body."""
    number = await _order(client, supplier_payload)
    result = await client.post(
        "/inbound/email", json=_payload(body=f"Our invoice for {number} follows.")
    )
    assert result.status_code == 200

    detail = (await client.get(f"/purchase-orders/{number}")).json()
    assert detail["documents"] == []
    assert any(i["kind"] == "missing_document" for i in detail["issues"])


@pytest.mark.asyncio
async def test_the_same_file_twice_on_one_message_is_stored_once(client, supplier_payload):
    number = await _order(client, supplier_payload)
    result = await client.post(
        "/inbound/email",
        json=_payload(
            [("invoice.pdf", "application/pdf", PDF), ("invoice-copy.pdf", "application/pdf", PDF)],
            body=f"Please find the invoice attached for {number}.",
        ),
    )
    assert len(result.json()["attachments"]) == 1


@pytest.mark.asyncio
async def test_a_calendar_invite_is_kept_but_not_filed_as_a_document(client, supplier_payload):
    """Stored, because everything that arrives is stored. Not filed, because a
    meeting invite is not the invoice the payment will be checked against."""
    number = await _order(client, supplier_payload)
    result = await client.post(
        "/inbound/email",
        json=_payload(
            [("meeting.ics", "text/calendar", b"BEGIN:VCALENDAR\nEND:VCALENDAR")],
            body=f"Our invoice for {number} follows.",
        ),
    )
    assert len(result.json()["attachments"]) == 1

    detail = (await client.get(f"/purchase-orders/{number}")).json()
    assert detail["documents"] == []
    assert any(i["kind"] == "missing_document" for i in detail["issues"])


@pytest.mark.asyncio
async def test_a_file_is_stored_even_when_the_message_cannot_be_read(client, supplier_payload):
    """Evidence kept only when the reading succeeded is evidence missing from
    exactly the cases somebody will later want to check."""
    await _order(client, supplier_payload)
    result = await client.post(
        "/inbound/email",
        json=_payload(
            [("something.pdf", "application/pdf", PDF)],
            body="Attached.",  # no PO number, no keyword
            subject="FW",
        ),
    )
    payload = result.json()
    assert payload["confidence"] < 0.7
    assert len(payload["attachments"]) == 1


@pytest.mark.asyncio
async def test_bad_base64_is_refused_rather_than_stored_empty(client):
    result = await client.post(
        "/inbound/email",
        json={
            "external_id": "<x@y>",
            "body": "hello",
            "attachments": [{"filename": "a.pdf", "content_base64": "not base64!!"}],
        },
    )
    assert result.status_code == 422


@pytest.mark.asyncio
async def test_downloading_an_unknown_attachment_is_a_404(client):
    assert (await client.get("/inbound/attachments/nope")).status_code == 404
