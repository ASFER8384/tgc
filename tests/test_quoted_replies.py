"""A reply carries our own email underneath it.

Found in use rather than in a test: a supplier raised their price, and the check
stayed silent because the order total was sitting in the quoted original below
their signature. Everything the extractor reads has to come from what the
supplier wrote this time.
"""

from sca.inbound.parser import parse, strip_quoted

# The shape Gmail produces, and the shape that broke it.
GMAIL_REPLY = """Dear Procurement,

Due to the war our raw material and freight costs have risen sharply since
we quoted. We confirm the order and will ship on 2026-09-28, at a revised
total of 145,700.00 SAR.

Best regards,
Asfer

On Tue, 11 Aug 2026 at 17:20, Procurement <buyer@example.com> wrote:
> Please accept the following order, PO-5002.
> Order total: 132,460.00 SAR
> Requested delivery: 2026-09-01
"""


def test_only_the_new_price_is_read_not_the_quoted_original():
    found = parse("Re: PO-5002", GMAIL_REPLY)
    assert found.amounts == [145700.0]


def test_the_quoted_delivery_date_does_not_win():
    """Our requested date is in the quote. Theirs is the one that counts."""
    found = parse("Re: PO-5002", GMAIL_REPLY)
    assert found.promised_date.isoformat() == "2026-09-28"


def test_outlook_style_quoting_is_cut_too():
    body = (
        "We confirm and will ship 2026-09-28 at 145,700.00 SAR.\n\n"
        "-----Original Message-----\n"
        "Order total: 132,460.00 SAR\n"
    )
    assert parse("Re: PO-5002", body).amounts == [145700.0]


def test_a_header_block_reply_is_cut_too():
    body = (
        "We confirm at 145,700.00 SAR.\n\n"
        "From: Procurement <buyer@example.com>\n"
        "Sent: Tuesday\n"
        "Order total: 132,460.00 SAR\n"
    )
    assert parse("Re: PO-5002", body).amounts == [145700.0]


def test_angle_bracket_quotes_are_dropped_without_a_marker():
    body = "We confirm at 145,700.00 SAR.\n> Order total: 132,460.00 SAR\n"
    assert parse("Re: PO-5002", body).amounts == [145700.0]


def test_a_message_with_no_quote_is_untouched():
    body = "We confirm PO-5002 and will ship on 2026-09-28."
    assert strip_quoted(body) == body
    assert parse("Re: PO-5002", body).kind == "acknowledgement"


def test_our_own_please_accept_does_not_make_every_reply_an_acknowledgement():
    """The order we send says "Please accept the following order". Quoted back
    under a non committal reply, it must not be read as the supplier accepting."""
    body = (
        "Thanks, we will revert.\n\n"
        "On Tue, 11 Aug 2026 at 13:50, Procurement wrote:\n"
        "> Please accept the following order, PO-5006.\n"
    )
    assert parse("Re: PO-5006", body).kind == "unknown"


def test_a_wrapped_attribution_line_does_not_donate_its_date():
    """Found in use. Gmail breaks "On <date> ... wrote:" across two lines, and a
    pattern anchored to one line leaves the first half behind. The date in it was
    read as the supplier's promised delivery date, which they never gave."""
    body = (
        "I accept this order.\r\n\r\n\r\n"
        "On Tue, 11 Aug 2026 at 13:52, MOHAMED ASFER ALI N <asfar62891@gmail.com>\r\n"
        "wrote:\r\n\r\n"
        "> Requested delivery: 2026-09-01\r\n"
    )
    found = parse("Re: Purchase order PO-5006 - Asfer", body)
    assert found.kind == "acknowledgement"
    assert found.promised_date is None


def test_plain_agreement_is_read_as_agreement():
    """Found in use: "I accept this order" fell through as unknown, because the
    rule listed "accepted" and not the word a person actually types."""
    body = (
        "I accept this order.\n\n"
        "On Tue, 11 Aug 2026 at 13:50, Procurement wrote:\n"
        "> Please accept the following order, PO-5006.\n"
    )
    found = parse("Re: Purchase order PO-5006 - Asfer", body)
    assert found.kind == "acknowledgement"
    assert found.po_number == "PO-5006"


def test_the_po_number_still_resolves_from_the_subject_alone():
    """Stripping the quote removes one of the places the number appears."""
    found = parse("Re: PO-5002", GMAIL_REPLY)
    assert found.po_number == "PO-5002"
    assert found.confidence == 1.0
